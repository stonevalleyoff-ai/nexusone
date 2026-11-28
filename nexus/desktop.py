"""Interface graphique Tk de NEXUS.

Tout ce qui touche à Ollama tourne dans des fils d'exécution secondaires ;
les widgets ne sont manipulés que par le fil principal, via une file.
"""
from __future__ import annotations

from queue import Empty, Queue
import re
import threading
import tkinter as tk
from tkinter import messagebox, ttk

from . import __version__
from .assistant import Assistant, ToolUse, Turn, machine_context
from .config import Settings
from .memory import Store
from .models import AUTO
from .ollama import OllamaClient, OllamaError, pick_model
from .telemetry import Sample, Telemetry, gib
from .tools import Env, Toolbox
from .watch import Watcher, load_rules_file

BG = '#0d1520'
PANEL = '#132030'
FIELD = '#1a2b3f'
LINE = '#24384f'
INK = '#e6eef7'
MUTED = '#8ea3b8'
ACCENT = '#6cc7f0'
OK = '#5fd39a'
WARN = '#f2b84b'
BAD = '#ff7b72'
FONT = 'DejaVu Sans'
SYSTEM_RESERVE_GIB = 2.5  # bureau XFCE, navigateur, couche d'écriture Live


class Gauge(tk.Frame):
    def __init__(self, master, label: str):
        super().__init__(master, bg=PANEL)
        top = tk.Frame(self, bg=PANEL)
        top.pack(fill='x')
        self.label = tk.Label(top, text=label, bg=PANEL, fg=MUTED, font=(FONT, 9))
        self.label.pack(side='left')
        self.value = tk.Label(top, text='—', bg=PANEL, fg=INK, font=(FONT, 9))
        self.value.pack(side='right')
        self.bar = tk.Canvas(self, height=6, bg=FIELD, highlightthickness=0)
        self.bar.pack(fill='x', pady=(3, 0))

    def set(self, text: str, fraction: float | None, colour: str = ACCENT) -> None:
        self.value.configure(text=text)
        self.bar.delete('all')
        if fraction is not None:
            width = max(1, self.bar.winfo_width())
            self.bar.create_rectangle(0, 0, width * max(0.0, min(1.0, fraction)), 6,
                                      fill=colour, width=0)


def level_colour(fraction: float | None, warn: float, bad: float) -> str:
    if fraction is None:
        return ACCENT
    return BAD if fraction >= bad else WARN if fraction >= warn else ACCENT


AUTO_LABEL = 'Automatique'      # dans la liste des modèles : NEXUS choisit lui-même


class NexusDesktop:
    def __init__(self, root: tk.Tk, settings: Settings):
        self.root, self.settings = root, settings
        self.events: Queue = Queue()
        self.client = settings.open_client()
        if getattr(self.client, 'engine', None) is not None:   # moteur colibrì tenu par NEXUS
            self.client.on_progress = lambda text: self.events.put(('status', text))
        self.store = Store(settings.db_path)
        self.chooser = settings.open_chooser(self.client)
        self.auto_model = ''      # le modèle que le choix automatique utilise pour dialoguer
        self.library = settings.open_library(self.client, lambda: self.chooser.pick('fond').name)
        self.agenda = settings.open_agenda()
        self.mailbox = settings.open_mailbox(self.client, self.chooser)
        toolbox = Toolbox(Env(library=self.library, agenda=self.agenda, mailbox=self.mailbox))
        self.assistant = Assistant(self.client, self.store, num_ctx=settings.num_ctx, toolbox=toolbox,
                                   souvenirs=settings.open_souvenirs(self.client), agenda=self.agenda,
                                   skills=settings.open_skills(toolbox), chooser=self.chooser)
        self.telemetry = Telemetry()
        self.watcher = Watcher(load_rules_file(settings.rules_path))
        self.sample: Sample | None = None
        self.models: dict[str, int] = {}
        self.connected = False
        self.conversation: int | None = None
        self.cancel = threading.Event()
        self.generating = False
        self.checking = False

        root.title(f'NEXUS {__version__}')
        root.geometry('1120x720')
        root.minsize(860, 560)
        root.configure(bg=BG)
        root.protocol('WM_DELETE_WINDOW', self.close)
        style = ttk.Style(root)
        style.theme_use('clam')
        style.configure('TCombobox', fieldbackground=FIELD, background=FIELD, foreground=INK,
                        arrowcolor=INK, bordercolor=LINE, lightcolor=FIELD, darkcolor=FIELD)
        style.map('TCombobox', fieldbackground=[('readonly', FIELD)], foreground=[('readonly', INK)])
        root.option_add('*TCombobox*Listbox.background', FIELD)
        root.option_add('*TCombobox*Listbox.foreground', INK)
        style.configure('Vertical.TScrollbar', background=LINE, troughcolor=PANEL, bordercolor=PANEL,
                        lightcolor=LINE, darkcolor=LINE, arrowcolor=MUTED, gripcount=0)
        style.map('Vertical.TScrollbar', background=[('active', MUTED)])

        self._build_sidebar()
        self._build_main()
        self._welcome()
        self.refresh_conversations()
        self.root.after(100, self._poll)
        self.root.after(200, self._tick)
        self.root.after(300, self.check_ollama)
        self._start_planner()

    # -- construction --------------------------------------------------------
    def _button(self, master, text, command, primary=False):
        return tk.Button(master, text=text, command=command, relief='flat', bd=0,
                         bg='#1f7fa8' if primary else LINE, fg=INK,
                         activebackground=ACCENT, activeforeground=BG,
                         font=(FONT, 10, 'bold' if primary else 'normal'),
                         padx=14, pady=6, cursor='hand2', highlightthickness=0)

    def _section(self, master, title):
        tk.Label(master, text=title.upper(), bg=PANEL, fg=MUTED,
                 font=(FONT, 8, 'bold')).pack(anchor='w', pady=(18, 6))

    def _build_sidebar(self):
        side = tk.Frame(self.root, bg=PANEL, width=270, padx=18, pady=16)
        side.pack(side='left', fill='y')
        side.pack_propagate(False)
        tk.Label(side, text='◉ NEXUS', bg=PANEL, fg=ACCENT, font=(FONT, 20, 'bold')).pack(anchor='w')
        tk.Label(side, text='IA locale · rien ne quitte la machine', bg=PANEL, fg=MUTED,
                 font=(FONT, 9)).pack(anchor='w')

        self._section(side, 'Machine')
        self.gauges = {}
        for key, label in (('cpu', 'Processeur'), ('mem', 'Mémoire'), ('disk', 'Espace'),
                           ('temp', 'Température'), ('battery', 'Batterie')):
            gauge = Gauge(side, label)
            self.gauges[key] = gauge
        for key in ('cpu', 'mem', 'disk'):
            self.gauges[key].pack(fill='x', pady=4)

        self._section(side, 'Alertes')
        self.alerts = tk.Label(side, text='Aucune alerte', bg=PANEL, fg=MUTED, justify='left',
                               anchor='w', wraplength=230, font=(FONT, 9))
        self.alerts.pack(fill='x')

        self._section(side, 'Assistant')
        self._button(side, 'Ce que NEXUS sait de moi', self.open_memory).pack(fill='x')
        self._button(side, 'Savoir-faire', self.open_skills).pack(fill='x', pady=(6, 0))
        self._button(side, 'Agenda', self.open_agenda).pack(fill='x', pady=(6, 0))
        self._button(side, 'Activité de NEXUS', self.open_activity).pack(fill='x', pady=(6, 0))
        self._button(side, 'Tâches planifiées', self.open_tasks).pack(fill='x', pady=(6, 0))
        self.notes_button = self._button(side, 'Notifications', self.open_notifications)
        self.notes_button.pack(fill='x', pady=(6, 0))

        self._section(side, 'Conversations')
        buttons = tk.Frame(side, bg=PANEL)
        buttons.pack(side='bottom', fill='x', pady=(8, 0))
        self._button(buttons, 'Nouvelle', self.new_conversation).pack(side='left')
        self._button(buttons, 'Tout effacer', self.forget_all).pack(side='right')
        self.session_note = tk.Label(side, text='', bg=PANEL, fg=WARN, justify='left',
                                     wraplength=230, font=(FONT, 8))
        self.session_note.pack(side='bottom', fill='x', pady=(8, 0))
        self.history = tk.Listbox(side, bg=FIELD, fg=INK, selectbackground=LINE,
                                  selectforeground=INK, relief='flat', highlightthickness=0,
                                  activestyle='none', font=(FONT, 9), bd=0)
        self.history.pack(fill='both', expand=True)
        self.history.bind('<<ListboxSelect>>', self.open_conversation)
        self.history_ids: list[int] = []

    def _build_main(self):
        main = tk.Frame(self.root, bg=BG, padx=20, pady=16)
        main.pack(side='left', fill='both', expand=True)

        top = tk.Frame(main, bg=BG)
        top.pack(fill='x')
        tk.Label(top, text='Modèle', bg=BG, fg=MUTED, font=(FONT, 10)).pack(side='left')
        self.model = tk.StringVar()
        self.model_box = ttk.Combobox(top, textvariable=self.model, state='readonly', width=28)
        self.model_box.pack(side='left', padx=10)
        self.model_box.bind('<<ComboboxSelected>>', lambda e: self.model_changed())
        self.dot = tk.Label(top, text='●', bg=BG, fg=MUTED, font=(FONT, 12))
        self.dot.pack(side='left', padx=(8, 4))
        self.status = tk.Label(top, text='Recherche du moteur d\'IA…', bg=BG, fg=MUTED, font=(FONT, 10))
        self.status.pack(side='left')
        self.use_tools = tk.BooleanVar(value=self.settings.tools)
        tk.Checkbutton(top, text='Consulter la machine', variable=self.use_tools, bg=BG, fg=MUTED,
                       selectcolor=FIELD, activebackground=BG, activeforeground=INK,
                       highlightthickness=0, bd=0, font=(FONT, 9)).pack(side='right')

        self.banner = tk.Label(main, text='', bg='#3a2a10', fg=WARN, justify='left', anchor='w',
                               padx=12, pady=8, font=(FONT, 9), wraplength=760)

        bottom = tk.Frame(main, bg=BG)
        bottom.pack(side='bottom', fill='x', pady=(12, 0))
        side = tk.Frame(bottom, bg=BG)  # placé avant le champ, sinon le champ prend toute la largeur
        side.pack(side='right', padx=(10, 0), fill='y')
        self.send_button = self._button(side, 'Envoyer', self.send, primary=True)
        self.send_button.pack(fill='x', ipady=6)
        tk.Label(side, text='Maj+Entrée :\nsaut de ligne', bg=BG, fg=MUTED,
                 font=(FONT, 8)).pack(pady=(6, 0))
        box = tk.Frame(bottom, bg=FIELD, padx=10, pady=8)
        box.pack(side='left', fill='x', expand=True)
        self.entry = tk.Text(box, height=3, bg=FIELD, fg=INK, insertbackground=INK, relief='flat',
                             wrap='word', font=(FONT, 11), highlightthickness=0, bd=0)
        self.entry.pack(fill='x')
        self.entry.bind('<Return>', self._on_return)
        self.entry.focus_set()

        frame = tk.Frame(main, bg=PANEL)
        frame.pack(fill='both', expand=True, pady=(12, 0))
        self.chat_frame = frame
        scroll = ttk.Scrollbar(frame, orient='vertical')
        scroll.pack(side='right', fill='y')
        self.transcript = tk.Text(frame, bg=PANEL, fg=INK, relief='flat', wrap='word',
                                  padx=18, pady=14, font=(FONT, 11), state='disabled',
                                  highlightthickness=0, bd=0, yscrollcommand=scroll.set,
                                  spacing3=4, cursor='arrow')
        self.transcript.pack(fill='both', expand=True)
        scroll.configure(command=self.transcript.yview)
        self.transcript.tag_configure('who_user', foreground=ACCENT, font=(FONT, 9, 'bold'), spacing1=14)
        self.transcript.tag_configure('who_nexus', foreground=OK, font=(FONT, 9, 'bold'), spacing1=14)
        self.transcript.tag_configure('user', foreground=INK)
        self.transcript.tag_configure('nexus', foreground=INK)
        self.transcript.tag_configure('note', foreground=MUTED, font=(FONT, 9, 'italic'))
        self.transcript.tag_configure('tool', foreground=ACCENT, font=(FONT, 9))
        self.transcript.tag_configure('h1', foreground=ACCENT, font=(FONT, 13, 'bold'), spacing1=6, spacing3=4)
        self.transcript.tag_configure('h2', foreground=INK, font=(FONT, 11, 'bold'), spacing1=8, spacing3=2)
        self.transcript.tag_configure('bold', font=(FONT, 11, 'bold'))
        self.transcript.tag_configure('bullet', lmargin1=14, lmargin2=28)
        self.transcript.tag_configure('error', foreground=BAD)

    # -- transcript ------------------------------------------------------------
    def _write(self, text: str, tag: str) -> None:
        self.transcript.configure(state='normal')
        self.transcript.insert('end', text, tag)
        self.transcript.configure(state='disabled')
        self.transcript.see('end')

    def _message(self, role: str, text: str) -> None:
        who, tag = ('VOUS', 'user') if role == 'user' else ('NEXUS', 'nexus')
        self._write(f'{who}\n', 'who_' + tag)
        self._write(text + '\n', tag)

    def _clear(self) -> None:
        self.transcript.configure(state='normal')
        self.transcript.delete('1.0', 'end')
        self.transcript.configure(state='disabled')

    def _welcome(self) -> None:
        self._clear()
        self._write('NEXUS\n', 'who_nexus')
        self._write('Bonjour. Je tourne entièrement sur cet ordinateur : vos questions ne partent '
                    'pas sur Internet. Je peux expliquer, rédiger, résumer vos documents, et examiner '
                    'la machine (programmes, réseau, matériel, erreurs, disques) pour vous aider à '
                    'diagnostiquer un problème. Je ne fais que lire : je ne modifie jamais rien.\n', 'nexus')

    # -- Ollama -------------------------------------------------------------------
    def _set_status(self, text: str, colour: str = MUTED) -> None:
        self.status.configure(text=text)
        self.dot.configure(fg=colour)

    def check_ollama(self) -> None:
        if self.checking:
            return
        self.checking = True
        threading.Thread(name='nexus-check_worker', target=self._check_worker, daemon=True).start()

    def _check_worker(self) -> None:
        try:
            # Seulement les modèles qui dialoguent (pas le modèle de sens bge-m3).
            self.events.put(('models', {c.name: c.size for c in self.chooser.candidates()}))
        except OllamaError as exc:
            self.events.put(('offline', str(exc)))

    def model_changed(self) -> None:
        self._memory_banner()
        model = self.model.get()
        if model:
            self._set_status('NEXUS choisit son modèle…' if model == AUTO_LABEL
                             else f'Chargement de {model} en mémoire…', WARN)
            threading.Thread(name='nexus-load_worker', target=self._load_worker, args=(model,), daemon=True).start()

    def _ready_text(self) -> str:
        label = self.model.get()
        return f'Prêt · automatique ({self.auto_model})' if label == AUTO_LABEL and self.auto_model else f'Prêt · {label}'

    def _load_worker(self, label: str) -> None:
        try:
            model = label
            if label == AUTO_LABEL:
                choice = self.chooser.pick('dialogue')
                model = self.auto_model = choice.name
                self.events.put(('status', f'Chargement de {model} ({choice.reason})…'))
            self.assistant.info(model)
            self.client.load(model)
            ahead = getattr(self.client, 'reads_ahead_for', None)
            if ahead(model) if ahead is not None else getattr(self.client, 'reads_ahead', False):
                self.events.put(('status', f'{model} lit ses consignes (une fois par démarrage, quelques minutes)…'))
                try:
                    self.assistant.warm_up(model, use_tools=self.settings.tools)
                except OllamaError:
                    pass   # lecture d'avance interrompue (bouton Arrêter) : la question la refera
            self.events.put(('loaded', label))
        except OllamaError as exc:
            self.events.put(('load_failed', str(exc)))

    def _memory_banner(self) -> None:
        size = self.models.get(self.model.get(), 0)
        total = self.sample.mem_total_mib if self.sample else None
        message = ''
        if not self.connected:
            message = ''
        elif not self.models:
            message = ('Aucun modèle n\'est installé. Avec une connexion Internet, ouvrez un terminal '
                       'et tapez : ollama pull qwen3:1.7b')
        elif size and total and size / 2**30 + SYSTEM_RESERVE_GIB > total / 1024:
            message = (f'Ce modèle ({size / 2**30:.1f} Go) risque de ne pas tenir dans la mémoire de '
                       f'cette machine ({total / 1024:.1f} Go). Choisissez-en un plus petit si les '
                       'réponses échouent ou si tout ralentit.')
        if message:
            self.banner.configure(text=message)
            self.banner.pack(fill='x', pady=(12, 0), before=self.chat_frame)
        else:
            self.banner.pack_forget()

    # -- conversations -------------------------------------------------------------
    def refresh_conversations(self) -> None:
        rows = self.store.conversations()
        self.history.delete(0, 'end')
        self.history_ids = [row['id'] for row in rows]
        for row in rows:
            self.history.insert('end', ' ' + row['title'])
        if self.conversation in self.history_ids:
            index = self.history_ids.index(self.conversation)
            self.history.selection_set(index)

    def open_conversation(self, _event=None) -> None:
        selection = self.history.curselection()
        if self.generating or not selection:
            return
        self.conversation = self.history_ids[selection[0]]
        self._clear()
        for message in self.store.messages(self.conversation):
            self._message(message['role'], message['content'])

    def new_conversation(self) -> None:
        if self.generating:
            return
        self.conversation = None
        self.history.selection_clear(0, 'end')
        self._welcome()
        self.entry.focus_set()

    # -- tâches planifiées -------------------------------------------------------------
    def _start_planner(self) -> None:
        """Vérifie les tâches chaque minute, dans un fil qui ne référence pas la fenêtre."""
        from .planner import Runner
        self.planner_stop = threading.Event()
        self.planner = self.settings.open_planner()
        if self.planner is None:
            return
        self.autonomy = self.settings.open_autonomy(self.library, self.agenda, self.planner)
        from .journal import publisher
        backup = self.settings.open_backup()
        health = self.settings.open_health(engine=self.settings.engine_probe(self.client, self.chooser),
                                           planner=self.planner, mailbox=self.mailbox, backup=backup)
        self.publish = publisher(self.settings, client=self.client, chooser=self.chooser, library=self.library,
                                 agenda=self.agenda, autonomy=self.autonomy, souvenirs=self.assistant.souvenirs,
                                 mailbox=self.mailbox, health=health)
        self.runner = Runner(self.planner, toolbox=self.assistant.toolbox, skills=self.assistant.skills,
                             souvenirs=self.assistant.souvenirs, autonomy=self.autonomy, journal=self.publish,
                             mailbox=self.mailbox, backup=backup, health=health)
        runner, planner, stop, events = self.runner, self.planner, self.planner_stop, self.events

        def loop() -> None:
            while not stop.is_set():
                try:
                    before = planner.unread()
                    runner.tick()
                    fresh = planner.notifications(unread_only=True, limit=1)
                    events.put(('notes', fresh[0]['titre'] if planner.unread() > before and fresh else ''))
                except Exception:  # le planificateur ne doit jamais s'arrêter ni faire tomber l'interface
                    pass
                stop.wait(60)

        threading.Thread(target=loop, name='planificateur', daemon=True).start()

    def _update_notes_button(self) -> None:
        count = self.planner.unread() if getattr(self, 'planner', None) else 0
        self.notes_button.configure(text=f'Notifications ({count})' if count else 'Notifications',
                                    bg='#1f7fa8' if count else LINE)

    def open_agenda(self) -> None:
        """Rendez-vous et choses à faire ; ajout par une phrase (« Dentiste demain à 14h »)."""
        if self.agenda is None:
            return
        from datetime import datetime
        from .agenda import quick_add
        window = tk.Toplevel(self.root, bg=BG, padx=18, pady=16)
        window.title('Agenda')
        window.geometry('700x560')
        tk.Label(window, text='Agenda', bg=BG, fg=ACCENT, font=(FONT, 14, 'bold')).pack(anchor='w')
        tk.Label(window, text='Ajoute par une phrase : « Dentiste demain à 14h », « acheter du pain », « raye le pain ». '
                              'Les rappels arrivent dans Notifications.',
                 bg=BG, fg=MUTED, font=(FONT, 9), wraplength=660, justify='left').pack(anchor='w', pady=(2, 8))
        row = tk.Frame(window, bg=BG)
        row.pack(fill='x')
        entry = tk.Entry(row, bg=FIELD, fg=INK, insertbackground=INK, relief='flat', font=(FONT, 11))
        entry.pack(side='left', fill='x', expand=True, ipady=6)
        message = tk.Label(window, text='', bg=BG, fg=ACCENT, font=(FONT, 9), wraplength=660, justify='left')
        message.pack(anchor='w', pady=(6, 0))
        frame = tk.Frame(window, bg=PANEL)
        frame.pack(fill='both', expand=True, pady=(8, 0))
        text = tk.Text(frame, bg=PANEL, fg=INK, relief='flat', wrap='word', padx=14, pady=10, font=(FONT, 10),
                       highlightthickness=0, bd=0)
        text.pack(fill='both', expand=True)

        def show() -> None:
            text.configure(state='normal')
            text.delete('1.0', 'end')
            text.insert('end', self.agenda.overview(datetime.now(), 30))
            text.configure(state='disabled')

        def add(_event=None) -> None:
            phrase = entry.get().strip()
            if phrase:
                message.configure(text=quick_add(self.agenda, phrase, datetime.now()).replace('**', ''))
                entry.delete(0, 'end')
                show()

        self._button(row, 'Ajouter', add).pack(side='left', padx=(8, 0))
        entry.bind('<Return>', add)
        show()
        entry.focus_set()

    def open_activity(self) -> None:
        """Ce que NEXUS a fait de lui-même ; chaque geste se défait d'un clic."""
        autonomy = getattr(self, 'autonomy', None)
        if autonomy is None:
            return
        window = tk.Toplevel(self.root, bg=BG, padx=18, pady=16)
        window.title('Activité de NEXUS')
        window.geometry('760x560')
        tk.Label(window, text='Activité de NEXUS', bg=BG, fg=ACCENT, font=(FONT, 14, 'bold')).pack(anchor='w')
        tk.Label(window, text='Ce que NEXUS a fait de lui-même, et ce qu\'il propose. Rien n\'est supprimé : tout se défait. '
                              'Niveaux d\'autonomie : nexus activite niveau.',
                 bg=BG, fg=MUTED, font=(FONT, 9), wraplength=720, justify='left').pack(anchor='w', pady=(2, 10))
        body = tk.Frame(window, bg=BG)
        body.pack(fill='both', expand=True)

        def refresh(message: str = '') -> None:
            for child in body.winfo_children():
                child.destroy()
            if message:
                tk.Label(body, text=message, bg=BG, fg=ACCENT, font=(FONT, 9), wraplength=720,
                         justify='left').pack(anchor='w', pady=(0, 8))
            acts = autonomy.acts(12)
            if not acts:
                tk.Label(body, text='Rien pour l\'instant.', bg=BG, fg=MUTED, font=(FONT, 10)).pack(anchor='w')
            for act in acts:
                row = tk.Frame(body, bg=PANEL, padx=10, pady=6)
                row.pack(fill='x', pady=(0, 6))
                faded = act['etat'] in ('annule', 'refuse', 'perime', 'echec')
                state = {'propose': 'Proposition : ', 'annule': 'Annulé : ', 'refuse': 'Écarté : ',
                         'perime': 'Périmé : ', 'echec': 'Sans succès : '}.get(act['etat'], '')
                tk.Label(row, text=state + act['resume'], bg=PANEL, fg=MUTED if faded else INK, font=(FONT, 10),
                         wraplength=520, justify='left').pack(side='left', anchor='w')
                if act['etat'] == 'fait':
                    self._button(row, 'Annuler', lambda a=act['id']: refresh(autonomy.undo(a))).pack(side='right')
                elif act['etat'] == 'propose':
                    self._button(row, 'Non', lambda a=act['id']: (autonomy.dismiss(a), refresh('Écarté.'))).pack(side='right')
                    self._button(row, 'Oui', lambda a=act['id']: refresh(autonomy.confirm(a))).pack(side='right', padx=(0, 6))

        refresh()

    def open_notifications(self) -> None:
        if getattr(self, 'planner', None) is None:
            return
        window = tk.Toplevel(self.root, bg=BG, padx=18, pady=16)
        window.title('Notifications')
        window.geometry('700x560')
        tk.Label(window, text='Notifications', bg=BG, fg=ACCENT, font=(FONT, 14, 'bold')).pack(anchor='w')
        frame = tk.Frame(window, bg=PANEL)
        frame.pack(fill='both', expand=True, pady=(10, 0))
        scroll = ttk.Scrollbar(frame, orient='vertical')
        scroll.pack(side='right', fill='y')
        text = tk.Text(frame, bg=PANEL, fg=INK, relief='flat', wrap='word', padx=14, pady=10, font=(FONT, 10),
                       highlightthickness=0, bd=0, yscrollcommand=scroll.set)
        text.pack(fill='both', expand=True)
        scroll.configure(command=text.yview)
        colours = {'critical': BAD, 'warning': WARN, 'info': ACCENT}
        for level, colour in colours.items():
            text.tag_configure(level, foreground=colour, font=(FONT, 11, 'bold'), spacing1=12)
        text.tag_configure('date', foreground=MUTED, font=(FONT, 8))
        notes = self.planner.notifications(limit=40)
        if not notes:
            text.insert('end', 'Aucune notification pour l\'instant.')
        for note in notes:
            text.insert('end', ('● ' if not note['lue'] else '') + note['titre'] + '\n',
                        note['niveau'] if note['niveau'] in colours else 'info')
            text.insert('end', note['creee'].replace('T', ' ') + '\n', 'date')
            body = re.sub(r'^#+\s*', '', note['texte'], flags=re.M)
            text.insert('end', body + '\n')
        text.configure(state='disabled')
        self.planner.mark_read()
        self._update_notes_button()

    def open_tasks(self) -> None:
        if getattr(self, 'planner', None) is None:
            return
        window = tk.Toplevel(self.root, bg=BG, padx=18, pady=16)
        window.title('Tâches planifiées')
        window.geometry('700x520')
        tk.Label(window, text='Tâches planifiées', bg=BG, fg=ACCENT, font=(FONT, 14, 'bold')).pack(anchor='w')
        tk.Label(window, text='Elles ne font que lire et prévenir (Notifications). Elles tournent tant que NEXUS est '
                              'ouvert ; sur le futur serveur maison, en permanence.',
                 bg=BG, fg=MUTED, font=(FONT, 9), wraplength=660, justify='left').pack(anchor='w', pady=(2, 10))
        body = tk.Frame(window, bg=BG)
        body.pack(fill='both', expand=True)

        def toggle(task) -> None:
            if not task['active'] and task['type'] == 'tempo' and not messagebox.askyesno(
                    'Veille Tempo', 'Chaque soir, NEXUS lira sur Internet la couleur Tempo du lendemain '
                    '(service communautaire api-couleur-tempo.fr). Aucune donnée personnelle n\'est envoyée.\n\n'
                    'Activer ?', parent=window):
                return
            self.planner.set_active(task['id'], not task['active'])
            refresh()

        def run_now(task, button) -> None:
            button.configure(text='En cours…', state='disabled')
            runner, events = self.runner, self.events

            def work() -> None:
                try:
                    result = runner.run_now(task['id'])
                except Exception as exc:
                    result = f'échec : {exc}'
                events.put(('notes', f"{task['nom']} : {result}"))

            threading.Thread(target=work, name='nexus-tache', daemon=True).start()
            window.after(1500, refresh)

        def refresh() -> None:
            for child in body.winfo_children():
                child.destroy()
            for task in self.planner.tasks():
                box = tk.Frame(body, bg=PANEL, padx=10, pady=8)
                box.pack(fill='x', pady=3)
                state = 'active' if task['active'] else 'inactive'
                tk.Label(box, text=f"{task['nom']}   ({state})", bg=PANEL, fg=INK if task['active'] else MUTED,
                         font=(FONT, 10, 'bold')).pack(anchor='w')
                detail = task['horaire'].describe()
                if task['derniere']:
                    detail += f" · dernière fois {task['derniere'].replace('T', ' ')} : {task['resultat'] or '—'}"
                tk.Label(box, text=detail, bg=PANEL, fg=MUTED, font=(FONT, 8), wraplength=640,
                         justify='left').pack(anchor='w')
                row = tk.Frame(box, bg=PANEL)
                row.pack(anchor='e')
                tk.Button(row, text='Désactiver' if task['active'] else 'Activer', relief='flat', bd=0,
                          bg=LINE if task['active'] else '#1f7fa8', fg=INK, padx=10, highlightthickness=0,
                          command=lambda t=task: toggle(t)).pack(side='left', padx=3)
                now_button = tk.Button(row, text='Lancer maintenant', relief='flat', bd=0, bg=LINE, fg=INK,
                                       padx=10, highlightthickness=0)
                now_button.configure(command=lambda t=task, b=now_button: run_now(t, b))
                now_button.pack(side='left', padx=3)

        refresh()

    def _mark_reply_start(self) -> None:
        self.transcript.mark_set('reply_start', 'end-1c')
        self.transcript.mark_gravity('reply_start', 'left')  # reste avant le texte qui arrive

    def _render_reply(self) -> None:
        """Remet en forme la réponse terminée : titres, gras, puces (Markdown simple)."""
        try:
            start = self.transcript.index('reply_start')
        except tk.TclError:
            return
        text = self.transcript.get(start, 'end-1c')
        if not re.search(r'^#{1,3} |\*\*|^\s*[-*] ', text, re.M):
            return
        self.transcript.configure(state='normal')
        self.transcript.delete(start, 'end-1c')
        for line in text.split('\n'):
            heading = re.match(r'^(#{1,3})\s+(.*)$', line)
            if heading:
                self.transcript.insert('end', heading.group(2).replace('**', '') + '\n',
                                       'h1' if len(heading.group(1)) == 1 else 'h2')
                continue
            bullet = re.match(r'^(\s*)[-*]\s+(.*)$', line)
            tags = ('nexus', 'bullet') if bullet else ('nexus',)
            if bullet:
                line = '• ' + bullet.group(2)
            for index, part in enumerate(re.split(r'\*\*', line)):
                if part:
                    self.transcript.insert('end', part, tags + (('bold',) if index % 2 else ()))
            self.transcript.insert('end', '\n', 'nexus')
        self.transcript.delete('end-2c', 'end-1c')  # pas de ligne vide en trop
        self.transcript.configure(state='disabled')
        self.transcript.see('end')

    # -- mémoire -----------------------------------------------------------------------
    def _propose_memory(self, souvenir_id: int, text: str) -> None:
        """« Retenir : … ? [Retenir] [Non] », directement dans la conversation."""
        souvenirs = self.assistant.souvenirs
        line = tk.Frame(self.transcript, bg=PANEL)
        label = tk.Label(line, text=f'Retenir « {text} » ?', bg=PANEL, fg=ACCENT, font=(FONT, 9),
                         wraplength=520, justify='left')
        label.pack(side='left')

        def answer(keep: bool) -> None:
            (souvenirs.confirm if keep else souvenirs.reject)(souvenir_id)
            for child in line.winfo_children()[1:]:
                child.destroy()
            label.configure(text=('Retenu : « ' if keep else 'Non retenu : « ') + text + ' »', fg=MUTED)

        for caption, keep in (('Retenir', True), ('Non', False)):
            tk.Button(line, text=caption, command=lambda k=keep: answer(k), relief='flat', bd=0,
                      bg=LINE, fg=INK, font=(FONT, 9), padx=8, highlightthickness=0,
                      cursor='hand2').pack(side='left', padx=(8, 0))
        self.transcript.configure(state='normal')
        self.transcript.window_create('end', window=line)
        self.transcript.insert('end', '\n')
        self.transcript.configure(state='disabled')
        self.transcript.see('end')

    def _propose_skill(self, skill_id: str) -> None:
        book = self.assistant.skills
        line = tk.Frame(self.transcript, bg=PANEL)
        label = tk.Label(line, text='Activer ce savoir-faire ?', bg=PANEL, fg=ACCENT, font=(FONT, 9))
        label.pack(side='left')

        def answer(keep: bool) -> None:
            (book.confirm if keep else book.delete)(skill_id)
            for child in line.winfo_children()[1:]:
                child.destroy()
            label.configure(text='Savoir-faire activé.' if keep else 'Savoir-faire supprimé.', fg=MUTED)

        for caption, keep in (('Activer', True), ('Supprimer', False)):
            tk.Button(line, text=caption, command=lambda k=keep: answer(k), relief='flat', bd=0, bg=LINE,
                      fg=INK, font=(FONT, 9), padx=8, highlightthickness=0).pack(side='left', padx=(8, 0))
        self.transcript.configure(state='normal')
        self.transcript.window_create('end', window=line)
        self.transcript.insert('end', '\n')
        self.transcript.configure(state='disabled')
        self.transcript.see('end')

    def open_skills(self) -> None:
        """Fenêtre « Savoir-faire » : recettes lisibles, Lancer / Activer / Supprimer."""
        book, toolbox = self.assistant.skills, self.assistant.toolbox
        labels = {name: toolbox.label(name) for name in toolbox.tools}
        window = tk.Toplevel(self.root, bg=BG, padx=18, pady=16)
        window.title('Savoir-faire')
        window.geometry('680x560')
        tk.Label(window, text='Savoir-faire', bg=BG, fg=ACCENT, font=(FONT, 14, 'bold')).pack(anchor='w')
        tk.Label(window, text='Des recettes en plusieurs étapes, en lecture seule. Pour en créer une : pose ta '
                              'question, puis dis « enregistre ça comme savoir-faire ».',
                 bg=BG, fg=MUTED, font=(FONT, 9), wraplength=640, justify='left').pack(anchor='w', pady=(2, 10))
        body = tk.Frame(window, bg=BG)
        body.pack(fill='both', expand=True)

        def launch(skill) -> None:
            window.destroy()
            self.entry.delete('1.0', 'end')
            self.entry.insert('1.0', f'Lance le savoir-faire {skill.nom}')
            self.send()

        def refresh() -> None:
            for child in body.winfo_children():
                child.destroy()
            for skill in book.all():
                box = tk.Frame(body, bg=PANEL, padx=10, pady=8)
                box.pack(fill='x', pady=3)
                state = {'integre': 'intégré', 'utilisateur': 'créé par toi'}[skill.origine]
                if skill.statut != 'valide':
                    state += ' · à activer'
                tk.Label(box, text=f'{skill.nom}   ({state})', bg=PANEL, fg=INK,
                         font=(FONT, 10, 'bold')).pack(anchor='w')
                tk.Label(box, text=skill.recipe(labels).split('\n', 1)[1], bg=PANEL, fg=MUTED, font=(FONT, 8),
                         justify='left', wraplength=620).pack(anchor='w')
                row = tk.Frame(box, bg=PANEL)
                row.pack(anchor='e')
                if skill.statut == 'valide':
                    tk.Button(row, text='Lancer', relief='flat', bd=0, bg='#1f7fa8', fg=INK, padx=10,
                              highlightthickness=0, command=lambda s=skill: launch(s)).pack(side='left', padx=3)
                else:
                    tk.Button(row, text='Activer', relief='flat', bd=0, bg=LINE, fg=INK, padx=10, highlightthickness=0,
                              command=lambda s=skill: (book.confirm(s.id), refresh())).pack(side='left', padx=3)
                if skill.origine == 'utilisateur':
                    tk.Button(row, text='Supprimer', relief='flat', bd=0, bg=LINE, fg=INK, padx=10,
                              highlightthickness=0,
                              command=lambda s=skill: (book.delete(s.id), refresh())).pack(side='left', padx=3)

        refresh()

    def open_memory(self) -> None:
        """Fenêtre « Ce que NEXUS sait de moi » : tout se lit et s'efface."""
        from .souvenirs import CATEGORIES
        souvenirs = self.assistant.souvenirs
        if souvenirs is None:
            messagebox.showinfo('Mémoire', 'La mémoire est désactivée (NEXUS_MEMOIRE=0).', parent=self.root)
            return
        window = tk.Toplevel(self.root, bg=BG, padx=18, pady=16)
        window.title('Ce que NEXUS sait de moi')
        window.geometry('620x460')
        tk.Label(window, text='Ce que NEXUS sait de moi', bg=BG, fg=ACCENT, font=(FONT, 14, 'bold')).pack(anchor='w')
        tk.Label(window, text='Dis « retiens que … » ou « oublie que … » dans la conversation. '
                              'Seul ce que tu dis ou valides est retenu.',
                 bg=BG, fg=MUTED, font=(FONT, 9), wraplength=580, justify='left').pack(anchor='w', pady=(2, 10))
        body = tk.Frame(window, bg=BG)
        body.pack(fill='both', expand=True)

        def refresh() -> None:
            for child in body.winfo_children():
                child.destroy()
            rows = souvenirs.list(None)
            if not rows:
                tk.Label(body, text='Rien pour l\'instant.', bg=BG, fg=MUTED, font=(FONT, 10)).pack(anchor='w')
            for row in rows:
                line = tk.Frame(body, bg=PANEL, padx=10, pady=6)
                line.pack(fill='x', pady=2)
                state = '' if row['statut'] == 'valide' else '  (proposé)'
                tk.Label(line, text=f"{CATEGORIES[row['categorie']]}{state}", bg=PANEL, fg=MUTED,
                         font=(FONT, 8)).pack(anchor='w')
                tk.Label(line, text=row['texte'], bg=PANEL, fg=INK, font=(FONT, 10), wraplength=440,
                         justify='left').pack(side='left', anchor='w')
                tk.Button(line, text='Effacer', relief='flat', bd=0, bg=LINE, fg=INK, padx=8,
                          highlightthickness=0, command=lambda i=row['id']: (souvenirs.delete(i), refresh())
                          ).pack(side='right')
                if row['statut'] == 'propose':
                    tk.Button(line, text='Retenir', relief='flat', bd=0, bg=LINE, fg=INK, padx=8,
                              highlightthickness=0, command=lambda i=row['id']: (souvenirs.confirm(i), refresh())
                              ).pack(side='right', padx=4)

        def forget_everything() -> None:
            if messagebox.askyesno('Tout oublier', 'Effacer définitivement toute la mémoire de NEXUS ?',
                                   parent=window):
                souvenirs.forget_all()
                refresh()

        self._button(window, 'Tout oublier', forget_everything).pack(anchor='e', pady=(10, 0))
        refresh()

    def forget_all(self) -> None:
        if self.generating:
            return
        if not messagebox.askyesno('Tout effacer', 'Effacer définitivement toutes les conversations ?',
                                   parent=self.root):
            return
        self.store.delete_all()
        self.new_conversation()
        self.refresh_conversations()

    # -- dialogue -------------------------------------------------------------------
    def _on_return(self, event):
        if event.state & 0x0001:  # Maj enfoncée : saut de ligne normal
            return None
        self.send()
        return 'break'

    def send(self) -> None:
        if self.generating:
            self.cancel.set()
            self.client.abort()  # coupe aussi l'attente du premier mot
            self.send_button.configure(text='Arrêt…')
            return
        question = self.entry.get('1.0', 'end').strip()
        model = self.model.get()
        shown = (self.auto_model or 'NEXUS') if model == AUTO_LABEL else model
        if not question:
            return
        if not self.connected or not model:
            self._write(f'{self.client.name} ou le modèle n\'est pas encore prêt. Patientez quelques secondes.\n', 'error')
            return
        if self.conversation is None:
            self.conversation = self.store.new_conversation()
        self.entry.delete('1.0', 'end')
        self._message('user', question)
        self._write('NEXUS\n', 'who_nexus')
        self._mark_reply_start()
        self.generating = True
        self.cancel.clear()
        self.send_button.configure(text='Arrêter')
        self._set_status(f'{shown} réfléchit…', WARN)
        model = AUTO if model == AUTO_LABEL else model
        context = machine_context(self.sample, self.watcher.active())
        threading.Thread(name='nexus-reply_worker', target=self._reply_worker,
                         args=(self.conversation, model, question, context, self.use_tools.get()),
                         daemon=True).start()

    def _reply_worker(self, conversation: int, model: str, question: str, context: str,
                      use_tools: bool) -> None:
        turn = Turn()
        try:
            for item in self.assistant.reply(conversation, model, question, context,
                                             cancelled=self.cancel.is_set, use_tools=use_tools,
                                             turn=turn):
                if isinstance(item, ToolUse):
                    self.events.put(('tool', item.label))
                else:
                    self.events.put(('chunk', item))
            self.events.put(('reply_done', turn))
        except OllamaError as exc:
            self.events.put(('reply_error', str(exc)))
        except Exception as exc:   # sinon le bouton resterait bloqué sur « Arrêter »
            self.events.put(('reply_error', f'Erreur interne ({type(exc).__name__}) : {exc}'))

    # -- boucle d'événements ---------------------------------------------------------
    def _poll(self) -> None:
        try:
            while True:
                kind, value = self.events.get_nowait()
                self._handle(kind, value)
        except Empty:
            pass
        self.root.after(60, self._poll)

    def _handle(self, kind: str, value) -> None:
        if kind == 'models':
            self.checking = False
            first = not self.connected
            self.connected, self.models = True, value
            names = ([AUTO_LABEL] if value else []) + list(value)
            self.model_box['values'] = names
            current = self.model.get()
            # Par défaut NEXUS choisit ; un modèle imposé (NEXUS_MODEL) ou pris dans la liste prime.
            chosen = current if current in names else (
                pick_model(list(value), self.settings.model) if self.settings.model else (AUTO_LABEL if value else None))
            if chosen:
                self.model.set(chosen)
                if first or chosen != current:
                    self.model_changed()
            else:
                self.model.set('')
                self._set_status(f'{self.client.name} prêt, mais aucun modèle installé', WARN)
            self._memory_banner()
        elif kind == 'notes':
            self._update_notes_button()
            if value:
                self._set_status(f'Nouvelle notification : {value}', ACCENT)
        elif kind == 'offline':
            self.checking = False
            self.connected = False
            managed = getattr(self.client, 'engine', None) is not None
            self._set_status(str(value)[:110] if managed else f'{self.client.name} ne répond pas — nouvel essai…', BAD)
            self.root.after(15000 if managed else 3000, self.check_ollama)
        elif kind == 'status':
            if value and not self.generating:
                self._set_status(value, WARN)
        elif kind == 'loaded':
            if value == self.model.get() and not self.generating:
                self._set_status(self._ready_text(), OK)
        elif kind == 'load_failed':
            self._set_status('Échec du chargement du modèle', BAD)
            self._write(f'{value}\n', 'error')
        elif kind == 'chunk':
            self._write(value, 'nexus')
        elif kind == 'tool':
            self._write(f'↳ consulte : {value}\n', 'tool')
            self._mark_reply_start()
            self._set_status(f'Consultation : {value}…', WARN)
        elif kind in ('reply_done', 'reply_error'):
            self.generating = False
            self.send_button.configure(text='Envoyer')
            if kind == 'reply_error':
                self._write(f'\n{value}\n', 'error')
                self._set_status('Erreur pendant la réponse', BAD)
                self.check_ollama()
            else:
                turn: Turn = value
                note = ' (interrompu)' if turn.cancelled else ''
                if turn.stats and turn.stats.tokens:
                    note += f'  ·  {turn.stats.tokens} jetons, {turn.stats.tokens_per_second:.1f}/s'
                    if turn.stats.done_reason == 'length':
                        note += ' — réponse coupée (longueur maximale)'
                if turn.model_reason and turn.model:      # choix automatique : dire quel modèle a répondu
                    note += f'  ·  {turn.model}'
                    self.auto_model = turn.model if 'approfondie' not in turn.model_reason else self.auto_model
                self._render_reply()
                self._write('\n', 'nexus')
                if turn.recalled:
                    self._write('Souvenirs utilisés : ' + ' · '.join(f'« {t} »' for t in turn.recalled) + '\n',
                                'tool')
                if turn.sources:
                    self._write('Sources consultées : ' + ' · '.join(turn.sources) + '\n', 'tool')
                for souvenir_id, text in turn.proposals:
                    self._propose_memory(souvenir_id, text)
                if turn.skill_proposal:
                    self._propose_skill(turn.skill_proposal)
                if note.strip():
                    self._write(note.strip(' ·') + '\n', 'note')
                self._set_status(self._ready_text(), OK)
            self.refresh_conversations()

    def _tick(self) -> None:
        engine = getattr(self.client, 'engine', None)
        if engine is not None and self.connected:
            engine.touch()   # fenêtre ouverte : le moteur reste prêt (pas de mise en veille)
        self.sample = s = self.telemetry.sample()
        self.watcher.update(s)
        if s.cpu_percent is not None:
            self.gauges['cpu'].set(f'{s.cpu_percent:.0f} %', s.cpu_percent / 100,
                                   level_colour(s.cpu_percent / 100, 0.85, 0.97))
        used = s.mem_used_percent
        if used is not None:
            self.gauges['mem'].set(f'{gib(s.mem_total_mib - s.mem_available_mib)} / {gib(s.mem_total_mib)}',
                                   used / 100, level_colour(used / 100, 0.8, 0.9))
        free = s.disk_free_percent
        if free is not None:
            self.gauges['disk'].label.configure(text='Espace de session' if s.live_session else 'Disque')
            self.gauges['disk'].set(f'{gib(s.disk_free_mib)} libres', 1 - free / 100,
                                    level_colour(1 - free / 100, 0.8, 0.9))
        if s.temp_c is not None:
            self.gauges['temp'].pack(fill='x', pady=4, after=self.gauges['disk'])
            self.gauges['temp'].set(f'{s.temp_c:.0f} °C', s.temp_c / 100,
                                    level_colour(s.temp_c / 100, 0.8, 0.9))
        if s.battery_percent is not None:
            self.gauges['battery'].pack(fill='x', pady=4, after=self.gauges['disk'])
            self.gauges['battery'].set(f'{s.battery_percent} %' + (' · batterie' if s.on_battery else ' · secteur'),
                                       s.battery_percent / 100,
                                       BAD if s.battery_percent < 15 else ACCENT)
        active = self.watcher.active()
        if active:
            self.alerts.configure(text='\n\n'.join(f'{a.text}\n{a.rule.advice}' for a in active),
                                  fg=BAD if any(a.rule.level == 'critical' for a in active) else WARN)
        else:
            self.alerts.configure(text='Aucune alerte', fg=MUTED)
        self.session_note.configure(text="Session Live sans persistance : tout est effacé à l'extinction."
                                    if s.live_session and not s.persistent else '')
        self.root.after(2000, self._tick)

    def close(self) -> None:
        self.planner_stop.set()
        self.cancel.set()
        self.client.abort()
        engine = getattr(self.client, 'engine', None)
        if engine is not None:   # le moteur démarré par cette fenêtre s'arrête avec elle
            threading.Thread(name='nexus-moteur-arret', target=engine.release, daemon=False).start()
        try:
            self.store.close()
        finally:
            self.root.destroy()


def main() -> None:
    root = tk.Tk()
    NexusDesktop(root, Settings.load())
    root.mainloop()


if __name__ == '__main__':
    main()
