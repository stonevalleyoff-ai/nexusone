"""Ligne de commande NEXUS.

  python3 -m nexus              interface graphique (défaut)
  python3 -m nexus status       état d'Ollama, des modèles et de la machine
  python3 -m nexus chat         dialogue dans le terminal
  python3 -m nexus ask "…"      une question, sans historique
  python3 -m nexus watch        affiche les alertes au fil de l'eau
  python3 -m nexus history      liste des conversations
  python3 -m nexus forget       efface toutes les conversations
  python3 -m nexus outils       outils de consultation (lecture seule) ; outil NOM pour en lancer un
  python3 -m nexus diagnostic   rapport complet de la machine, sans IA
  python3 -m nexus docs …       bibliothèque de documents : indexer, chercher, ranger, depenses
  python3 -m nexus souvenirs …  ce que NEXUS sait de toi : liste, ajouter, valider, oublier
  python3 -m nexus savoir-faire …  recettes en plusieurs étapes : liste, voir, lancer, valider
  python3 -m nexus taches …     tâches planifiées ; notifications : leurs messages ; planificateur : le service
  python3 -m nexus vpn …        tunnel privé sans tiers : installer ADRESSE, ajouter "Mon téléphone", retirer NOM
  python3 -m nexus mise-a-jour … versions officielles : verifier, installer (tests et retour arrière compris), annuler
  python3 -m nexus sante        ce que NEXUS constate sur lui-même, et quoi faire
  python3 -m nexus sauvegarde … copie chiffrée sur un autre disque : installer DOSSIER, maintenant, etat, restaurer DOSSIER
  python3 -m nexus service …    NEXUS démarre avec le PC, enfermé par systemd : installer, etat, journal, retirer
  python3 -m nexus alertes …    alertes sur téléphone verrouillé, sans tiers : activer, essai, discret oui|non, renouveler
  python3 -m nexus voix …       parler à NEXUS depuis le téléphone : installer, etat, essai FICHIER
  python3 -m nexus courrier …   courrier en lecture seule : ajouter gmail ADRESSE, relever, retirer NOM
  python3 -m nexus journal …    le journal du matin : composer le numéro du jour, liste, reglages imprimer oui
  python3 -m nexus activite …   ce que NEXUS a fait seul : liste, annuler N, valider N, tournee, niveau GESTE NIVEAU
  python3 -m nexus agenda …     rendez-vous, rappels, choses à faire : voir, ajouter "Dentiste demain à 14h", fait "pain"
  python3 -m nexus modeles …    les modèles locaux, celui que NEXUS choisit et pourquoi ; evaluer : les mesurer ici
  python3 -m nexus serveur …    serveur maison et appli téléphone : lancer (--reseau), appairer, appareils, revoquer
  python3 -m nexus moteur …     moteur colibrì (gros modèle sur petit PC) : etat, installer, demarrer, arreter, journal
"""
from __future__ import annotations

import argparse
from dataclasses import replace
from pathlib import Path
import json
import sys
import time

from . import __version__
from .assistant import Assistant, ToolUse, Turn, machine_context
from .config import Settings
from .memory import Store
from .models import AUTO
from .ollama import OllamaClient, OllamaError, model_name, pick_model
from .telemetry import Telemetry, describe
from .tools import Env, Toolbox
from .watch import Watcher, load_rules_file


def _model(client: OllamaClient, settings: Settings, requested: str | None, role: str = 'dialogue') -> str:
    """Le modèle demandé (--model), sinon celui que NEXUS choisit pour ce rôle."""
    if requested:
        installed = [m['name'] for m in client.models()]
        model_name(requested)
        chosen = pick_model(installed, requested)
        if chosen is None or (chosen != requested and chosen != requested + ':latest'):
            raise OllamaError(f'Modèle {requested!r} absent. Installés : {", ".join(installed) or "aucun"}')
        return chosen
    return settings.open_chooser(client).pick(role).name


def _sample_now(telemetry: Telemetry):
    telemetry.sample()
    time.sleep(0.5)  # deux relevés pour mesurer l'activité du processeur
    return telemetry.sample()


def cmd_status(args, settings: Settings) -> int:
    client = settings.open_client()
    sample = _sample_now(Telemetry())
    watcher = Watcher(load_rules_file(settings.rules_path))
    watcher.update(sample)
    report: dict = {'nexus': __version__, 'machine': describe(sample),
                    'alertes': [a.text for a in watcher.active()]}
    engine = getattr(client, 'engine', None)
    try:
        if engine is not None and not hasattr(client, 'big_model') and not engine.running():   # ne pas charger 10 Go pour un simple état
            found = engine.installed()
            if found is None:
                raise OllamaError('pas installé (nexus moteur installer)')
            report['ollama'] = {'version': 'arrêté, démarre à la première question', 'adresse': client.base_url,
                                'modèles': [found.model_id], 'modèle_préféré': settings.model}
        else:
            report['ollama'] = {'version': client.version(), 'adresse': client.base_url,
                                'modèles': [m['name'] + (f" ({m['size'] / 2**30:.1f} Go)" if m['size'] else '')
                                            for m in client.models()],
                                'modèle_préféré': settings.model}
        if not settings.model:      # NEXUS choisit : dire quoi, et pourquoi
            roles = settings.open_chooser(client, sample.mem_total_mib, sample.cores).roles()
            report['choix'] = {role: {'modèle': choice.name, 'raison': choice.reason} for role, choice in roles.items()}
    except OllamaError as exc:
        report.setdefault('ollama', {'erreur': str(exc)})
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f'NEXUS {__version__}')
        for line in report['machine']:
            print('  ' + line)
        for text in report['alertes']:
            print('  ALERTE : ' + text)
        ollama = report['ollama']
        if 'erreur' in ollama:
            print(f'{client.name} : ' + ollama['erreur'])
        else:
            print(f"{client.name} ({ollama['version']}) sur {ollama['adresse']}")
            for name in ollama['modèles'] or ['aucun modèle installé']:
                print('  ' + name)
        for role, label in (('dialogue', 'Pour te répondre'), ('fond', 'Pour le travail de fond')):
            if role in report.get('choix', {}):
                print(f"{label} : {report['choix'][role]['modèle']} — {report['choix'][role]['raison']}")
    return 0 if 'erreur' not in report['ollama'] else 1


def _answer(assistant: Assistant, conversation: int, model: str, question: str,
            telemetry: Telemetry, watcher: Watcher, use_tools: bool = True, interactive: bool = False,
            deep: bool = False) -> None:
    sample = telemetry.sample()
    watcher.update(sample)
    turn = Turn()
    try:
        for item in assistant.reply(conversation, model, question,
                                    machine_context(sample, watcher.active()),
                                    use_tools=use_tools, turn=turn, deep=deep):
            if isinstance(item, ToolUse):
                sys.stdout.write(f'[consulte : {item.label}]\n')
            else:
                sys.stdout.write(item)
            sys.stdout.flush()
    except KeyboardInterrupt:
        print('\n[interrompu]')
        return
    if turn.skill_proposal:
        print(f'\n  Pour l\'activer : nexus savoir-faire valider {turn.skill_proposal}', end='')
    if turn.recalled:
        print('\n  Souvenirs utilisés : ' + ' · '.join(f'« {t} »' for t in turn.recalled), end='')
    if turn.sources:
        print('\n  Sources consultées : ' + ' · '.join(turn.sources), end='')
    if turn.stats and turn.stats.tokens:
        chosen = f' · {turn.model} : {turn.model_reason}' if turn.model_reason else ''
        print(f'\n  ({turn.stats.tokens} jetons, {turn.stats.tokens_per_second:.1f}/s{chosen})')
    else:
        print()
    for souvenir_id, text in turn.proposals:
        if interactive:
            try:
                keep = input(f'  Retenir « {text} » ? [o/N] ').strip().lower() in ('o', 'oui', 'y')
            except EOFError:
                keep = False
            if keep:
                assistant.souvenirs.confirm(souvenir_id)
                print('  Retenu.')
            else:
                assistant.souvenirs.reject(souvenir_id)
        else:
            print(f'  NEXUS propose de retenir « {text} » : nexus souvenirs valider {souvenir_id}')


def cmd_chat(args, settings: Settings) -> int:
    client = settings.open_client()
    chooser = settings.open_chooser(client)
    model = _model(client, settings, args.model) if args.model else AUTO
    telemetry, watcher = Telemetry(), Watcher(load_rules_file(settings.rules_path))
    telemetry.sample()
    with Store(settings.db_path) as store:
        library = settings.open_library(client, lambda: chooser.pick('fond').name)
        agenda = settings.open_agenda()
        toolbox = Toolbox(Env(library=library, agenda=agenda, mailbox=settings.open_mailbox()))
        assistant = Assistant(client, store, num_ctx=settings.num_ctx, toolbox=toolbox, chooser=chooser, agenda=agenda,
                              souvenirs=settings.open_souvenirs(client), skills=settings.open_skills(toolbox))
        conversation = store.new_conversation()
        shown = model if model != AUTO else f"{chooser.pick('dialogue').name} (choisi par NEXUS)"
        print(f'NEXUS · {shown} · /quitter pour sortir, /nouveau pour repartir de zéro')
        while True:
            try:
                question = input('\nVous > ').strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return 0
            if question in ('/quitter', '/exit', '/quit'):
                return 0
            if question == '/nouveau':
                conversation = store.new_conversation()
                print('Nouvelle conversation.')
                continue
            if question:
                print('NEXUS > ', end='')
                try:
                    _answer(assistant, conversation, model, question, telemetry, watcher,
                            settings.tools and not args.sans_outils, interactive=True)
                except OllamaError as exc:
                    print(f'\nErreur : {exc}')


def cmd_ask(args, settings: Settings) -> int:
    client = settings.open_client()
    chooser = settings.open_chooser(client)
    model = _model(client, settings, args.model) if args.model else AUTO
    telemetry, watcher = Telemetry(), Watcher(load_rules_file(settings.rules_path))
    telemetry.sample()
    with Store(':memory:') as store:  # rien n'est conservé
        library = settings.open_library(client, lambda: chooser.pick('fond').name)
        agenda = settings.open_agenda()
        toolbox = Toolbox(Env(library=library, agenda=agenda, mailbox=settings.open_mailbox()))
        assistant = Assistant(client, store, num_ctx=settings.num_ctx, toolbox=toolbox, chooser=chooser, agenda=agenda,
                              souvenirs=settings.open_souvenirs(client), skills=settings.open_skills(toolbox))
        _answer(assistant, store.new_conversation(), model, args.question, telemetry, watcher,
                settings.tools and not args.sans_outils, deep=args.approfondi)
    return 0


def cmd_watch(args, settings: Settings) -> int:
    telemetry, watcher = Telemetry(), Watcher(load_rules_file(settings.rules_path))
    telemetry.sample()
    print(f'Surveillance ({len(watcher.rules)} règles, relevé toutes les {args.interval:g} s). Ctrl+C pour arrêter.')
    try:
        while True:
            time.sleep(args.interval)
            for change in watcher.update(telemetry.sample()):
                stamp = time.strftime('%H:%M:%S')
                if change.kind == 'raised':
                    print(f'{stamp}  ▲ {change.alert.rule.level:8} {change.alert.text} — {change.alert.rule.advice}')
                else:
                    print(f'{stamp}  ▼ rétabli  {change.alert.rule.id}')
    except KeyboardInterrupt:
        return 0


def cmd_history(args, settings: Settings) -> int:
    with Store(settings.db_path) as store:
        rows = store.conversations()
    for row in rows:
        print(f"{row['id']:>4}  {row['updated_at'][:16].replace('T', ' ')}  {row['messages']:>3} msg  {row['title']}")
    if not rows:
        print('Aucune conversation enregistrée.')
    return 0


def cmd_forget(args, settings: Settings) -> int:
    with Store(settings.db_path) as store:
        count = store.delete_all()
    print(f'{count} conversation(s) effacée(s).')
    return 0


def _runner(settings: Settings):
    from .planner import Runner
    client = settings.open_client(timeout=900)
    from .journal import publisher
    chooser = settings.open_chooser(client)
    library = settings.open_library(client, lambda: chooser.pick('fond').name)
    agenda = settings.open_agenda()
    mailbox = settings.open_mailbox(client, chooser)
    toolbox = Toolbox(Env(library=library, agenda=agenda, mailbox=mailbox))
    planner = settings.open_planner()
    autonomy = settings.open_autonomy(library, agenda, planner)
    souvenirs = settings.open_souvenirs(client)
    backup = settings.open_backup()
    health = settings.open_health(engine=settings.engine_probe(client, chooser), planner=planner, mailbox=mailbox,
                                  backup=backup)
    journal = publisher(settings, client=client, chooser=chooser, library=library, agenda=agenda, autonomy=autonomy,
                        souvenirs=souvenirs, mailbox=mailbox, health=health)
    return planner, Runner(planner, toolbox=toolbox, skills=settings.open_skills(toolbox), souvenirs=souvenirs,
                           autonomy=autonomy, journal=journal, mailbox=mailbox, backup=backup, health=health,
                           updater=settings.open_updater())


def cmd_taches(args, settings: Settings) -> int:
    planner, runner = _runner(settings)
    action, value = args.action, (args.valeur or '').strip()
    if action == 'liste':
        for task in planner.tasks():
            state = 'active ' if task['active'] else 'inactive'
            last = f"  · dernière : {task['derniere']} ({task['resultat'] or '—'})" if task['derniere'] else ''
            print(f"  [{state}] {task['id']:24} {task['nom']} — {task['horaire'].describe()}{last}")
        print('\nActiver : nexus taches activer ID · Lancer maintenant : nexus taches lancer ID')
    elif action in ('activer', 'desactiver'):
        if action == 'activer' and value == 'veille-tempo':
            print('La veille Tempo lit chaque soir la couleur du lendemain sur Internet '
                  '(api-couleur-tempo.fr). Aucune donnée personnelle n\'est envoyée.')
        print('Fait.' if planner.set_active(value, action == 'activer') else 'Tâche inconnue.')
    elif action == 'lancer':
        print(runner.run_now(value))
        for note in planner.notifications(unread_only=True, limit=3):
            print(f"\n▶ {note['titre']}\n{note['texte']}")
    elif action == 'horaire':
        from .planner import Schedule
        schedule = json.loads(args.horaire or '{}')
        print(f"Nouvel horaire : {Schedule.parse(schedule).describe()}" if planner.set_schedule(value, schedule)
              else 'Tâche inconnue.')
    return 0


def cmd_notifications(args, settings: Settings) -> int:
    planner = settings.open_planner()
    notes = planner.notifications(unread_only=not args.toutes, limit=20)
    if not notes:
        print('Aucune notification' + ('.' if args.toutes else ' non lue.'))
    for note in reversed(notes):
        flag = '' if note['lue'] else '● '
        print(f"\n{flag}{note['creee'].replace('T', ' ')}  {note['titre']}\n{note['texte']}")
    planner.mark_read()
    return 0


def cmd_vpn(args, settings: Settings) -> int:
    """Le tunnel privé de la maison (WireGuard), sans service tiers."""
    vpn = settings.open_vpn()
    if args.action == 'installer':
        if not args.valeur:
            raise ValueError('donner l\'adresse publique de la maison : nexus vpn installer 203.0.113.7 '
                             '(elle se lit dans l\'interface de la box)')
        vpn.setup(args.valeur, args.port)
        print('\n'.join(vpn.describe()))
        print('\nÀ faire une fois, sur ce PC (droits d\'administrateur) :')
        print('  sudo apt install wireguard-tools qrencode')
        for command in vpn.install_commands():
            print('  ' + command)
        print(f'Puis, dans l\'interface de la box : rediriger le port UDP {args.port} vers ce PC (seulement celui-là).')
        print('Ensuite : nexus vpn ajouter "Mon téléphone"   et   nexus serveur --vpn')
    elif args.action == 'ajouter':
        config = vpn.add_device(args.valeur or '')
        code = vpn.qr(config)
        print(f'Dans l\'appli WireGuard du téléphone : « + », puis « Scanner un code QR ».' if code else
              'Dans l\'appli WireGuard du téléphone : « + », puis « Créer à partir de zéro », et recopier ceci '
              '(ou : sudo apt install qrencode pour un code QR) :')
        print(code or config)
        print('Ce réglage contient la clé de l\'appareil : il n\'est affiché qu\'une fois et NEXUS ne le garde pas.')
        print('Pour que le PC connaisse ce nouvel appareil :')
        for command in vpn.reload_commands():
            print('  ' + command)
    elif args.action == 'retirer':
        if vpn.remove_device(args.valeur or ''):
            print('Appareil retiré. Pour que ce soit effectif tout de suite :')
            for command in vpn.reload_commands():
                print('  ' + command)
        else:
            print('Aucun appareil ne porte ce nom.')
            print('\n'.join(vpn.describe()))
    else:
        print('\n'.join(vpn.describe()))
    return 0


def cmd_mise_a_jour(args, settings: Settings) -> int:
    """Les versions de NEXUS : voir, installer (tests et retour arrière compris), annuler."""
    updater = settings.open_updater()
    if args.action == 'verifier':
        result = updater.check()
        if not result['ok']:
            print('Vérification impossible : ' + result['message'])
            return 1
        print('\n'.join(updater.describe()))
    elif args.action == 'installer':
        plan = updater.plan(args.valeur)
        installed = updater.installed()
        if installed['commit'] == plan['commit'] and updater.current.is_symlink():
            print(f"NEXUS {plan['version']} est déjà en place (empreinte {plan['commit'][:12]}).")
            return 0
        print(f"Version en place : {installed['version'] or '?'}"
              + (f" (empreinte {installed['commit'][:12]})" if installed['commit'] else ' (dossier de travail)'))
        print(f"Version à installer : {plan['version']}, empreinte {plan['commit']}")
        print(f'  depuis {updater.source}')
        if not args.oui:
            if not sys.stdin.isatty():
                raise ValueError('une mise à jour s\'approuve : relancer dans un terminal, ou ajouter --oui')
            if input('Installer exactement ce code ? [o/N] ').strip().lower() not in ('o', 'oui'):
                print('Rien n\'a été changé.')
                return 0
        first = not updater.current.is_symlink()
        result = updater.install(plan, skip_tests=args.sans_tests, backup=settings.open_backup())
        print(result['message'])
        if not result['ok']:
            return 1
        if result.get('notes'):
            print('Ce qui change :\n' + '\n'.join('  ' + line for line in result['notes'].splitlines()))
        service = settings.open_service()
        service.write_launcher(updater.current, updater.python)
        if result['redemarre']:
            print('Le service tourne sur la nouvelle version.')
        elif service.state()['installe'] and not first:
            print('Le service n\'était pas en marche : sudo systemctl start nexus')
        if first:
            print(f'La commande « nexus » ({service.launcher}) lance désormais les versions officielles.')
            print('Pour que le service les suive aussi, une dernière fois : nexus service installer, puis ses commandes sudo.')
        print('Revenir à la version d\'avant : nexus mise-a-jour annuler')
    elif args.action == 'annuler':
        back = updater.rollback()
        settings.open_service().write_launcher(updater.current, updater.python)
        print(f"Retour à NEXUS {back['version']}. Le service repart dessus de lui-même dans la minute.")
        print('(Si la version annulée avait transformé les bases, leurs copies d\'avant sont dans '
              f"{updater.data / 'avant-mise-a-jour'}.)")
    else:
        print('\n'.join(updater.describe()))
    return 0


def cmd_sante(args, settings: Settings) -> int:
    """Ce que NEXUS constate sur lui-même : moteur, tâches, courrier, sauvegarde, place, service."""
    client = settings.open_client(timeout=20)
    chooser = settings.open_chooser(client)
    planner = settings.open_planner()
    mailbox = settings.open_mailbox(client, chooser)
    try:
        health = settings.open_health(engine=settings.engine_probe(client, chooser), planner=planner, mailbox=mailbox,
                                      backup=settings.open_backup())
        findings = health.check()
        print('\n'.join(health.report()))
    finally:
        for resource in (planner, mailbox):
            if resource is not None:
                resource.close()
    return 1 if any(f.level == 'critical' for f in findings) else 0


def cmd_sauvegarde(args, settings: Settings) -> int:
    """La sauvegarde chiffrée : documents, réglages et données de NEXUS, sur un autre disque."""
    import getpass
    backup = settings.open_backup()
    if args.action == 'installer':
        if not args.valeur:
            raise ValueError('donner le dossier du disque de sauvegarde : nexus sauvegarde installer /media/toi/DISQUE')
        known = backup.key() is not None
        key = backup.setup(Path(args.valeur), getpass.getpass('Clé de la sauvegarde à reprendre : ') if args.reprendre else None)
        print('\n'.join(backup.describe()))
        if not known and not args.reprendre:
            print('\nLA CLÉ DE TA SAUVEGARDE, à recopier sur papier et à ranger ailleurs que près du PC :\n')
            print(f'    {key}\n')
            print('Sans elle, la sauvegarde est illisible, pour toi aussi. Elle reste sur ce PC pour les copies de nuit,')
            print('mais le jour où le PC meurt, seul ton papier permettra de tout retrouver. (La revoir : nexus sauvegarde cle)')
            if sys.stdin.isatty():      # on vérifie tout de suite que le papier est juste
                from .backup import tidy_key
                while tidy_key(input('Pour vérifier ton papier, retape la clé : ')) != key:
                    print('Ce n\'est pas la même : relis ton papier et corrige-le.')
                print('C\'est bien elle.')
        print('Première sauvegarde : nexus sauvegarde maintenant   (ensuite, chaque nuit à 4h15 si `nexus serveur` tourne)')
        if settings.open_service().state()['installe']:
            print('Le service est installé : relancer `nexus service installer` pour qu\'il ait le droit d\'écrire sur ce disque.')
    elif args.action == 'maintenant':
        if not backup.configured():
            raise ValueError('la sauvegarde n\'est pas réglée : nexus sauvegarde installer /chemin/du/disque')
        print('Sauvegarde en cours…', flush=True)
        result = backup.run()
        print(('Sauvegarde faite : ' if result['ok'] else 'ÉCHEC : ') + result['message'])
        return 0 if result['ok'] else 1
    elif args.action == 'verifier':
        print('Relecture de toute la sauvegarde (peut être long)…', flush=True)
        problem = backup.verify_all()
        print('La sauvegarde est entière : tout a été relu et vérifié.' if problem is None else 'PROBLÈME : ' + problem)
        return 0 if problem is None else 1
    elif args.action == 'restaurer':
        key = None
        if args.depuis and (backup.key() is None or args.reprendre):
            key = getpass.getpass('Clé de la sauvegarde (celle de ton papier) : ')
        source = Path(args.depuis) if args.depuis else None
        if args.valeur:      # dans un dossier à part : pour reprendre un fichier, ou regarder avant de décider
            result = backup.restore(Path(args.valeur), source=source, key=key, day=args.date)
            print(f"Sauvegarde du {str(result['fait_le']).replace('T', ' à ')} ressortie dans {result['dossier']} :")
            for label in result['places']:
                print(f"  {result['dossier'] / label}")
            print('Rien n\'a été écrasé : prends-y ce qu\'il te faut.')
        else:                # PC neuf ou vidé : NEXUS remet tout à sa place lui-même
            result = backup.restore_in_place(source=source, key=key, day=args.date)
            print(f"Sauvegarde du {str(result['fait_le']).replace('T', ' à ')} remise en place :")
            for label, place in result['places'].items():
                print(f'  {place}')
            for left in result['laisses']:
                print(f'  laissé tel quel, il existait déjà : {left}')
            if key is not None and source is not None:      # et les sauvegardes de nuit reprennent, même disque, même clé
                backup.setup(source.parent if source.name == 'NEXUS-sauvegarde' else source, key)
                print('Les sauvegardes de nuit reprendront sur ce disque, avec la même clé.')
    elif args.action == 'cle':
        print(backup.key() or 'Pas de clé : la sauvegarde n\'est pas réglée.')
    else:
        print('\n'.join(backup.describe() + (backup.describe_dates() if backup.configured() else [])))
    return 0


def cmd_service(args, settings: Settings) -> int:
    """NEXUS en service du PC : il démarre avec la machine, enfermé par systemd."""
    service = settings.open_service()
    if args.action == 'installer':
        mode = 'vpn' if args.vpn else 'reseau' if args.reseau else None
        if mode is None:      # rien de précisé : le réglage le plus sûr qui soit prêt
            mode = 'vpn' if settings.open_vpn().configured() else 'reseau'
        plan = service.prepare(mode, args.port)
        where = {'vpn': 'dans le tunnel privé seulement', 'reseau': 'sur le réseau de la maison',
                 'local': 'sur ce PC seulement'}[mode]
        print(f'Service préparé : NEXUS écoutera {where}, port {args.port}, sous le compte « {plan.user} ».')
        print('Il ne pourra écrire que dans :')
        for path in plan.writable:
            print(f'  {path}')
        print(f'La commande « nexus » est posée dans {service.launcher}.')
        if not Path('/run/systemd/system').is_dir():
            print('ATTENTION : cette machine ne démarre pas avec systemd, les commandes ci-dessous n\'y marcheront pas.')
        print('\nÀ lancer maintenant (droits d\'administrateur), et de nouveau seulement si `nexus service etat`')
        print('dit un jour que le service n\'est plus à jour :')
        for command in service.install_commands():
            print('  ' + command)
        print('Ensuite : nexus service etat   ·   nexus service journal')
        for line in service.describe()[1:]:
            print(line)
    elif args.action == 'retirer':
        print('Pour arrêter et retirer le service (tes données ne sont pas touchées) :')
        for command in service.remove_commands():
            print('  ' + command)
    elif args.action == 'journal':
        print(service.journal())
    else:
        print('\n'.join(service.describe()))
    return 0


def cmd_alertes(args, settings: Settings) -> int:
    """Les alertes sur téléphone verrouillé : relais pour l'appli ntfy, sans service tiers."""
    planner = settings.open_planner()
    relay = settings.open_relay(planner)
    if relay is None:
        raise ValueError('les alertes ont besoin du planificateur (tâches planifiées désactivées)')
    try:
        if args.action == 'activer':
            relay.enable()
        elif args.action == 'desactiver':
            relay.disable()
        elif args.action == 'renouveler':
            if not relay.enabled():
                raise ValueError('les alertes sont éteintes : nexus alertes activer')
            relay.renew()
            print('Sujet renouvelé : les téléphones reliés à l\'ancien ne reçoivent plus rien.')
        elif args.action == 'discret':
            if args.valeur not in ('oui', 'non'):
                raise ValueError('nexus alertes discret oui   ou   nexus alertes discret non')
            relay.set_discreet(args.valeur == 'oui')
        elif args.action == 'essai':
            if not relay.enabled():
                raise ValueError('les alertes sont éteintes : nexus alertes activer')
            planner.notify('info', 'Essai d\'alerte', 'Si tu lis ceci téléphone verrouillé, les alertes marchent.')
            print('Alerte d\'essai déposée : elle part vers le téléphone si `nexus serveur` tourne.')
        print('\n'.join(relay.describe()))
        if relay.enabled() and args.action in ('activer', 'renouveler', 'etat'):
            print('Sur le téléphone : installer l\'appli ntfy, puis dans l\'appli de NEXUS, onglet Maison,')
            print('« Relier l\'appli ntfy ». À la main : dans ntfy, « + », « Utiliser un autre serveur »,')
            print('adresse de NEXUS (https://10.73.0.1:8765 dans le tunnel) et le sujet ci-dessus.')
    finally:
        planner.close()
    return 0


def cmd_voix(args, settings: Settings) -> int:
    """La voix : moteur de transcription local (whisper.cpp), tenu par NEXUS."""
    voice = settings.open_voice()
    if args.action == 'installer':
        voice.install(args.valeur or 'small')
        print('Voix installée. Dans l\'appli du téléphone, un bouton micro apparaît à côté d\'« Envoyer ».')
    elif args.action == 'essai':
        if not args.valeur:
            raise ValueError('préciser un fichier audio : nexus voix essai enregistrement.wav')
        started = time.monotonic()
        text = voice.transcribe(Path(args.valeur).expanduser().read_bytes())
        print(f'« {text} »' if text else '(rien d\'intelligible)')
        print(f'  transcrit en {time.monotonic() - started:.1f} s')
    else:
        print('\n'.join(voice.describe()))
    return 0


def cmd_courrier(args, settings: Settings) -> int:
    """Le courrier en lecture seule : boîtes renseignées, relève, courriels importants."""
    from .mail import PRESETS
    if args.action == 'relever':
        _planner, runner = _runner(settings)
        print('Relève du courrier (lecture seule ; le tri par le modèle peut prendre plusieurs minutes)…')
        print(runner.run_now('courrier-nuit'))
        if runner.mailbox is not None:
            for problem in runner.mailbox.errors:
                print('  ' + problem)
            print(runner.mailbox.overview(days=3))
        return 0
    with settings.open_mailbox() as mailbox:
        if args.action == 'ajouter':
            kind, login = (args.valeur or '').lower(), args.adresse or ''
            if kind not in PRESETS or '@' not in login:
                raise ValueError(f"usage : nexus courrier ajouter {'|'.join(PRESETS)} ton.adresse@exemple.fr")
            import getpass
            print(PRESETS[kind]['aide'])
            password = getpass.getpass(f'Mot de passe pour {login} (il ne s\'affiche pas) : ').replace(' ', '')
            if not password:
                raise ValueError('mot de passe vide')
            name = {'gmail': 'Gmail', 'laposte': 'La Poste'}[kind]
            mailbox.add_account(name, PRESETS[kind]['serveur'], PRESETS[kind]['port'], login, password)
            print(f'{name} : connexion réussie, en lecture seule. Première relève cette nuit (ou : nexus courrier relever).')
        elif args.action == 'retirer':
            print('Boîte retirée, avec ce que NEXUS en avait retenu.' if mailbox.remove_account(args.valeur or '')
                  else 'Aucune boîte ne porte ce nom. Boîtes : ' + (', '.join(a['nom'] for a in mailbox.accounts()) or 'aucune'))
        else:
            for account in mailbox.accounts():
                print(f"  {account['nom']} : {account['identifiant']} (lecture seule)")
            print(mailbox.overview(days=7))
    return 0


def cmd_journal(args, settings: Settings) -> int:
    """Le journal du matin : composer le numéro du jour, voir les numéros, régler l'impression."""
    press = settings.open_press()
    if args.action == 'liste':
        for edition in press.editions():
            print(f"  {edition['jour']}  {edition['titre']}" + ('  (PDF)' if edition['pdf'] else ''))
        print(f'Dossier : {press.folder}' if press.editions() else 'Aucun numéro pour l\'instant : nexus journal')
        return 0
    if args.action == 'reglages':
        if args.valeur in ('imprimer', 'actualites', 'mot') and args.choix in ('oui', 'non'):
            press.set(**{args.valeur: args.choix == 'oui'})
        elif args.valeur == 'imprimante' and args.choix:
            press.set(imprimante=args.choix)
        elif args.valeur:
            raise ValueError('réglages : imprimer oui|non, actualites oui|non, mot oui|non, imprimante NOM')
        current = press.settings()
        print(f"  imprimer chaque matin : {'oui' if current['imprimer'] else 'non'}"
              f"{' (imprimante : ' + current['imprimante'] + ')' if current['imprimante'] else ' (imprimante par défaut)'}")
        print(f"  actualités : {'oui' if current['actualites'] else 'non'}")
        print(f"  mot du jour écrit par le modèle (essai) : {'oui' if current['mot'] else 'non'}")
        return 0
    from datetime import datetime
    from .journal import publisher
    client = settings.open_client(timeout=900)
    chooser = settings.open_chooser(client)
    library = settings.open_library(client, lambda: chooser.pick('fond').name)
    agenda, planner = settings.open_agenda(), settings.open_planner()
    mailbox = settings.open_mailbox(client, chooser)
    health = settings.open_health(engine=settings.engine_probe(client, chooser), planner=planner, mailbox=mailbox,
                                  backup=settings.open_backup())
    publish = publisher(settings, client=client, chooser=chooser, library=library, agenda=agenda,
                        autonomy=settings.open_autonomy(library, agenda, planner), souvenirs=settings.open_souvenirs(client),
                        mailbox=mailbox, health=health)
    print('Composition du numéro du jour (actualités comprises : une minute environ)…')
    result = publish(datetime.now())
    print(f"  Page : {result['html']}")
    if result['pdf']:
        print(f"  PDF  : {result['pdf']}" + ('  (envoyé à l\'imprimante)' if result['imprime'] else ''))
    for note in result['notes'] + result['edition'].notes:
        print('  ' + note)
    return 0


def cmd_activite(args, settings: Settings) -> int:
    """Ce que NEXUS fait de lui-même : journal, annulation, niveaux d'autonomie."""
    from .autonomy import GESTURES, LEVELS
    client = settings.open_client(timeout=900)
    chooser = settings.open_chooser(client)
    library = settings.open_library(client, lambda: chooser.pick('fond').name)
    autonomy = settings.open_autonomy(library, settings.open_agenda(), settings.open_planner())
    if autonomy is None:
        raise ValueError(f'dossier de documents introuvable : {settings.library_root}')
    number = int(args.valeur) if (args.valeur or '').isdigit() else None
    if args.action in ('annuler', 'valider', 'refuser'):
        if number is None:
            raise ValueError(f'préciser le numéro : nexus activite {args.action} N')
        print({'annuler': autonomy.undo, 'valider': autonomy.confirm,
               'refuser': lambda n: 'Proposition écartée.' if autonomy.dismiss(n) else 'Rien à écarter.'}[args.action](number))
    elif args.action == 'tournee':
        results = autonomy.tour()
        print('\n'.join(f"  {r['resume']}" + ('  (proposition)' if r['etat'] == 'propose' else '') for r in results)
              or 'Rien de nouveau dans le dossier d\'arrivée.')
    elif args.action == 'niveau':
        if args.valeur and args.niveau:
            autonomy.set_level(args.valeur, args.niveau)
        for gesture, label in GESTURES.items():
            print(f"  {gesture:12} {LEVELS[autonomy.levels()[gesture]]:20} {label}")
        if not (args.valeur and args.niveau):
            print(f"Pour changer : nexus activite niveau GESTE NIVEAU   (niveaux : {', '.join(LEVELS)})")
    else:
        marks = {'fait': 'fait    ', 'propose': 'PROPOSE ', 'annule': 'annulé  ', 'refuse': 'écarté  ',
                 'perime': 'périmé  ', 'echec': 'échoué  '}
        acts = autonomy.acts(30)
        for act in reversed(acts):
            print(f"{act['id']:4}  {act['quand'].replace('T', ' ')}  {marks.get(act['etat'], act['etat']):8} {act['resume']}")
        print('Aucun geste pour l\'instant.' if not acts else
              'Défaire : nexus activite annuler N    Répondre à une proposition : nexus activite valider N / refuser N')
    return 0


def cmd_agenda(args, settings: Settings) -> int:
    """Rendez-vous, rappels et choses à faire."""
    from datetime import datetime
    from .agenda import apply_command, quick_add
    with settings.open_agenda() as agenda:
        now = datetime.now()
        if args.action == 'ajouter':
            if not args.texte:
                raise ValueError('préciser quoi : nexus agenda ajouter "Dentiste demain à 14h"')
            print(quick_add(agenda, args.texte, now).replace('**', ''))
        elif args.action == 'fait':
            done = apply_command(agenda, f'Raye {args.texte or ""}', now) if args.texte else None
            print(done.replace('**', '') if done else 'Rien dans la liste ne correspond clairement. La voici :')
            if not done:
                print(agenda.overview(now, 1).split('Choses à faire', 1)[-1].lstrip(' :\n') or '(vide)')
        else:
            print(agenda.overview(now, args.jours))
    return 0


def cmd_modeles(args, settings: Settings) -> int:
    """Les modèles installés, ce que NEXUS en sait, et lequel il choisit pour quoi."""
    client = settings.open_client(timeout=1800)
    chooser = settings.open_chooser(client)
    if args.action == 'evaluer':
        from .evaluation import evaluate
        candidates = chooser.candidates()
        names = [c.name for c in candidates if c.fits]
        if args.valeur:
            names = [n for n in (args.valeur, args.valeur + ':latest') if n in [c.name for c in candidates]][:1]
            if not names:
                raise OllamaError(f'Modèle {args.valeur!r} absent. Installés : {", ".join(c.name for c in candidates)}')
        print(f'Mesure de {len(names)} modèle(s) sur cette machine (un petit modèle : une minute ; un gros : dix ou plus).')
        schemas = Toolbox().schemas()
        for name in names:
            try:
                result = evaluate(client, name, schemas, progress=_progress)
            except OllamaError as exc:
                _progress_end()
                client.unload(name)
                print(f'  {name} : mesure impossible ({exc})')
                continue
            _progress_end()
            client.unload(name)      # rendre la mémoire avant de mesurer le suivant
            chooser.measures.save(name, result)
            speed = f", {result['jetons_s']} jetons/s" if result['jetons_s'] else ''
            print(f"  {name} : bons choix d'outil {round(result['outils'] * result['essais'])}/{result['essais']}, "
                  f"réponse type en {result['secondes']:.0f} s{speed}")
        print()
    candidates = chooser.candidates()
    if not candidates:
        print('Aucun modèle de dialogue installé. Avec Internet : ollama pull qwen3.5:2b')
        return 1
    roles = chooser.roles()
    for c in sorted(candidates, key=lambda c: -c.quality):
        marks = [label for role, label in (('dialogue', 'DIALOGUE'), ('fond', 'TRAVAIL DE FOND')) if roles[role].name == c.name]
        engine = 'colibrì' if c.engine == 'colibri' else 'Ollama'
        print(f"  {c.name:28} {engine:8} {c.describe()}" + (f"   ← {' + '.join(marks)}" if marks else ''))
    print(f"\nPour te répondre : {roles['dialogue'].name} — {roles['dialogue'].reason}")
    print(f"Pour le travail de fond (classement, réponse approfondie) : {roles['fond'].name} — {roles['fond'].reason}")
    if any(not c.measured for c in candidates) and not settings.model:
        print('Certains modèles ne sont pas mesurés sur cette machine : nexus modeles evaluer')
    return 0


def cmd_serveur(args, settings: Settings) -> int:
    """NEXUS en serveur maison, avec l'appli pour le téléphone."""
    from .access import Access
    access_path = settings.db_path.parent / 'appareils.sqlite3'
    if args.action == 'appairer':
        with Access(access_path) as access:
            print(f'Code d\'appairage : {access.new_code()}')
        print('Valable 10 minutes, une seule fois. À saisir sur le téléphone, à l\'adresse affichée par `nexus serveur`.')
        return 0
    if args.action == 'appareils':
        with Access(access_path) as access:
            devices = access.devices()
        for d in devices:
            seen = (d['vu_le'] or 'jamais').replace('T', ' ')[:16]
            print(f"{d['id']:3}  {d['nom']:30}  appairé le {d['cree_le'][:10]}  vu : {seen}")
        if not devices:
            print('Aucun appareil appairé (nexus serveur appairer).')
        return 0
    if args.action == 'revoquer':
        if not (args.valeur or '').isdigit():
            raise ValueError('préciser le numéro : nexus serveur revoquer N (voir nexus serveur appareils)')
        with Access(access_path) as access:
            print('Appareil révoqué : il devra être appairé de nouveau.' if access.revoke(int(args.valeur))
                  else 'Aucun appareil ne porte ce numéro.')
        return 0
    from . import server
    from .vpn import SERVER_ADDRESS
    httpd = server.make_server(settings, network=args.reseau, port=args.port, vpn=args.vpn,
                               tls=False if args.sans_chiffrement else None, verbose=args.bavard)
    scheme = 'https' if httpd.tls else 'http'
    print(f'NEXUS {__version__} — serveur maison ({httpd.home.client.name})')
    if args.vpn or args.reseau:
        if args.vpn:
            print(f'  Dans le tunnel privé seulement : {scheme}://{SERVER_ADDRESS}:{args.port}')
            print('  (ni Internet ni le Wi-Fi de la maison ne voient ce serveur ; le téléphone passe par l\'appli WireGuard)')
        else:
            for address in server.local_addresses() or ['<adresse de ce PC>']:
                print(f'  Sur le téléphone (même Wi-Fi) : {scheme}://{address}:{args.port}')
        if httpd.tls:
            base = f'{scheme}://{SERVER_ADDRESS if args.vpn else (server.local_addresses() or ["<adresse>"])[0]}:{args.port}'
            print('  Pour que le téléphone fasse confiance à ce PC sans avertissement, installer une fois le certificat')
            print(f'  de la maison : ouvrir {base}/autorite.crt, puis Réglages > Sécurité > Installer un certificat > CA.')
            print('  Vérifier que son empreinte SHA-256 est bien celle-ci avant d\'accepter :')
            print('    ' + server.fingerprint(settings.db_path.parent / 'serveur' / 'autorite.pem'))
        else:
            print('  ATTENTION : sans chiffrement, un autre appareil du réseau peut lire les échanges.')
    else:
        print(f'  Sur ce PC seulement : {scheme}://127.0.0.1:{args.port}   (--reseau pour le téléphone)')
    print('  Nouvel appareil : nexus serveur appairer      Arrêt : Ctrl+C')
    httpd.home.start()
    httpd.home.updater.announce_running()

    def hand_over() -> None:      # une autre version a pris la place : on s'arrête, le service repart dessus
        httpd.home.replaced.wait()
        print('Une autre version de NEXUS est en place : ce serveur lui laisse la main.', flush=True)
        httpd.shutdown()

    import threading
    threading.Thread(name='nexus-passage-de-temoin', target=hand_over, daemon=True).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print('\nArrêt du serveur…')
    finally:
        httpd.server_close()
        httpd.home.close()
        httpd.access.close()
    return 0


def cmd_moteur(args, settings: Settings) -> int:
    """Le moteur colibrì tenu par NEXUS."""
    engine = settings.open_engine()
    if args.action == 'installer':
        found = engine.install(args.valeur)
        print(f'Moteur installé, modèle {found.model_id}. NEXUS l\'utilise dès maintenant : '
              'il démarre tout seul à la première question.')
    elif args.action == 'demarrer':
        client = settings.open_client(timeout=1800) if settings.backend != 'ollama' else None
        if getattr(client, 'engine', None) is None or engine.installed() is None:
            engine.ensure(_progress)
        else:   # démarrer, puis lire d'avance les consignes et les outils : la 1re question n'attendra pas
            big = getattr(client, 'big_model', None)
            model = big() if big is not None else pick_model([m['name'] for m in client.models()], settings.model)
            print('Le modèle lit ses consignes (une fois par démarrage, quelques minutes)…')
            Assistant(client, Store(':memory:'), num_ctx=settings.num_ctx,
                      toolbox=Toolbox() if settings.tools else None).warm_up(model, use_tools=settings.tools)
        print('Moteur prêt.')
    elif args.action == 'arreter':
        print('Moteur arrêté.' if engine.stop() else 'Le moteur ne tournait pas.')
    elif args.action == 'journal':
        print('\n'.join(engine.log_tail(40)) or 'Journal vide.')
    else:
        print('\n'.join(engine.describe()))
        if settings.backend == 'ollama' and engine.installed():
            print('  (non utilisé : NEXUS_BACKEND=ollama)')
    return 0


_progress_shown = False


def _progress(text: str) -> None:
    """Avancement du démarrage du moteur, sur la sortie d'erreur pour ne pas salir la réponse."""
    global _progress_shown
    if not text:
        return _progress_end()
    if sys.stderr.isatty():
        sys.stderr.write('\r' + text.ljust(78))
        sys.stderr.flush()
    elif not _progress_shown:
        print(text.split('…')[0] + '…', file=sys.stderr)
    _progress_shown = True


def _progress_end() -> None:
    global _progress_shown
    if _progress_shown and sys.stderr.isatty():
        sys.stderr.write('\r' + ' ' * 78 + '\r')
    _progress_shown = False


def cmd_planificateur(args, settings: Settings) -> int:
    """Boucle du service : vérifie les tâches chaque minute (à lancer via systemd sur le mini-PC)."""
    planner, runner = _runner(settings)
    print('Planificateur NEXUS démarré (Ctrl+C pour arrêter).', flush=True)
    try:
        while True:
            try:
                for task_id in runner.tick():
                    print(f"{time.strftime('%Y-%m-%d %H:%M')}  {task_id} : {planner.get(task_id)['resultat']}",
                          flush=True)
            except Exception as exc:   # un service permanent ne doit pas mourir sur une erreur passagère
                print(f"{time.strftime('%Y-%m-%d %H:%M')}  erreur du planificateur : {type(exc).__name__} : {exc}",
                      file=sys.stderr, flush=True)
            time.sleep(60)
    except KeyboardInterrupt:
        return 0


def cmd_skills(args, settings: Settings) -> int:
    toolbox = Toolbox()
    book = settings.open_skills(toolbox)
    labels = {name: toolbox.label(name) for name in toolbox.tools}
    action, value = args.action, (args.valeur or '').strip()
    if action == 'liste':
        for skill in book.all():
            state = '' if skill.statut == 'valide' else '  [proposé : à valider]'
            print(f'  {skill.id:22} {skill.nom}{state}\n  {"":22} {skill.description}')
    elif action == 'voir':
        skill = book.get(value)
        print(skill.recipe(labels) if skill else 'Savoir-faire inconnu.')
    elif action in ('valider', 'supprimer'):
        done = (book.confirm if action == 'valider' else book.delete)(value)
        print('Fait.' if done else 'Impossible (inconnu, déjà validé, ou savoir-faire intégré).')
    elif action == 'lancer':
        skill = book.get(value)
        if skill is None or skill.statut != 'valide':
            raise ValueError('savoir-faire inconnu ou pas encore validé')
        args.question, args.model, args.sans_outils = f'Lance le savoir-faire {skill.nom}', None, False
        return cmd_ask(args, settings)
    for error in book.errors:
        print(f'Fichier ignoré : {error}', file=sys.stderr)
    return 0


def cmd_souvenirs(args, settings: Settings) -> int:
    from .souvenirs import CATEGORIES, Souvenirs
    if settings.souvenirs_db is None:
        raise ValueError('mémoire non configurée')
    with Souvenirs(settings.souvenirs_db, settings.open_client(), embed_model=settings.embed_model) as mem:
        action, value = args.action, (args.valeur or '').strip()
        if action == 'liste':
            rows = mem.list(None)
            if not rows:
                print('NEXUS ne sait encore rien de toi. Dis-lui « retiens que … ».')
            for row in rows:
                state = '' if row['statut'] == 'valide' else '  [proposé]'
                print(f"{row['id']:>4}  {CATEGORIES[row['categorie']][:22]:22}  {row['texte']}{state}")
        elif action == 'ajouter':
            print(mem.add(value)[1] if value else 'Texte manquant : nexus souvenirs ajouter "…"')
        elif action in ('valider', 'refuser', 'oublier'):
            if not value.isdigit():
                raise ValueError(f'numéro attendu : nexus souvenirs {action} 12')
            done = {'valider': mem.confirm, 'refuser': mem.reject, 'oublier': mem.delete}[action](int(value))
            print('Fait.' if done else 'Aucun souvenir correspondant.')
        elif action == 'tout-oublier':
            answer = input('Effacer définitivement TOUTE la mémoire ? Taper « oui » : ').strip().lower()
            print(f'{mem.forget_all()} souvenir(s) effacé(s).' if answer == 'oui' else 'Annulé.')
        if mem.degraded:
            print('Attention : modèle de sens indisponible, recherche par mots seulement.', file=sys.stderr)
    return 0


def cmd_docs(args, settings: Settings) -> int:
    from .library import CATEGORIES, money
    if args.dossier:
        settings = replace(settings, library_root=Path(args.dossier).expanduser())
    client = settings.open_client(timeout=900)
    needs_llm = args.action == 'indexer' and not args.sans_classement
    model = _model(client, settings, args.model, role='fond') if needs_llm else None
    library = settings.open_library(client, model)
    if library is None:
        raise ValueError(f'dossier introuvable : {settings.library_root} (option --dossier ou NEXUS_BIBLIOTHEQUE)')
    with library:
        if args.action == 'indexer':
            print(f'Indexation de {library.root}' + (f' (classement par {model})' if model else ''))
            stats = library.scan(classify=needs_llm, progress=lambda m: print('  ' + m, flush=True))
            print('Terminé : ' + ', '.join(f'{v} {k}' for k, v in stats.items() if v))
            stats['reconnus_total'] = library.stats()['reconnus']
            if stats['sans_texte']:
                print('  Documents sans texte lisible : ' + ('rien de reconnaissable dedans (photo floue, page blanche ?).'
                      if library.ocr_languages else 'installer la reconnaissance de texte, puis relancer :\n'
                      '    sudo apt install tesseract-ocr tesseract-ocr-fra'))
            if stats.get('reconnus_total'):
                print(f"  {stats['reconnus_total']} document(s) lus par reconnaissance de texte (scans, photos) : "
                      'les chiffres peuvent contenir des erreurs de lecture.')
        elif args.action == 'liste':
            for d in library.documents():
                cat = CATEGORIES.get(d['category'] or '', '—')
                print(f"{d['doc_date'] or '          '}  {cat[:18]:18}  {money(d['amount_cents']):>12}  "
                      f"{(d['issuer'] or '')[:24]:24}  {d['path']}" + ('  [sans texte]' if d['status'] == 'sans_texte' else '')
                      + ('  [texte reconnu]' if d.get('ocr') == 1 else ''))
            print(library.stats())
        elif args.action == 'chercher':
            if not args.texte:
                raise ValueError('préciser la question : nexus docs chercher "…"')
            for r in library.search(args.texte, k=5):
                print(f"\n[{r['path']}, page {r['page']}]\n  " + r['text'][:400].replace('\n', '\n  '))
        elif args.action == 'ranger':
            pending = library.pending()
            if not pending:
                print('Rien à ranger : tous les documents classés sont à leur place proposée.')
                return 0
            for d in pending:
                print(f"  {d['path']}\n    → {d['proposal']}")
            if not args.appliquer:
                print(f'\n{len(pending)} proposition(s). Rien n\'a été déplacé. Pour appliquer : '
                      'nexus docs ranger --appliquer   (annulable avec : nexus docs annuler)')
                return 0
            moved = library.apply()
            print(f'\n{len(moved)} document(s) rangé(s). Pour annuler : nexus docs annuler')
        elif args.action == 'annuler':
            undone = library.undo()
            print(f'{len(undone)} déplacement(s) annulé(s).' if undone else 'Aucun rangement à annuler.')
            for path in library.not_undone:
                print(f'  Non annulé (fichier absent, déplacé à la main, ou ancien nom déjà repris) : {path}')
        elif args.action == 'depenses':
            from .tools import depenses
            year = int(args.texte) if args.texte and args.texte.isdigit() else 0
            print(depenses(Env(library=library), args.categorie or 'toutes', year, ''))
    return 0


def cmd_outils(args, settings: Settings) -> int:
    toolbox = Toolbox()
    print('Outils de consultation, en lecture seule (aucun ne modifie la machine) :')
    for tool in toolbox.tools.values():
        params = ', '.join(tool.params) or 'aucun paramètre'
        print(f'  {tool.name:22} {tool.description}  [{params}]')
    return 0


def cmd_diagnostic(args, settings: Settings) -> int:
    """Rapport complet de la machine, sans IA : tous les outils de consultation d'un coup."""
    toolbox = Toolbox()
    for name in ('infos_systeme', 'processus_gourmands', 'etat_reseau', 'materiel',
                 'firmwares_manquants', 'erreurs_recentes', 'stockage'):
        print(f'\n── {toolbox.label(name)} ' + '─' * max(4, 50 - len(toolbox.label(name))))
        print(toolbox.call(name, {}))
    return 0


def cmd_outil(args, settings: Settings) -> int:
    try:
        params = json.loads(args.arguments) if args.arguments else {}
    except json.JSONDecodeError as exc:
        raise ValueError('arguments : objet JSON attendu, ex. {"critere": "processeur"}') from exc
    print(Toolbox().call(args.nom, params))
    return 0


def cmd_desktop(args, settings: Settings) -> int:
    from .desktop import main as desktop_main  # tkinter n'est requis que pour l'interface
    desktop_main()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog='nexus', description='Assistant local et surveillance de la machine.')
    parser.add_argument('--version', action='version', version=f'NEXUS {__version__}')
    sub = parser.add_subparsers(dest='command')
    sub.add_parser('desktop', help='interface graphique (défaut)')
    p = sub.add_parser('status', help="état d'Ollama et de la machine")
    p.add_argument('--json', action='store_true')
    p = sub.add_parser('chat', help='dialogue dans le terminal')
    p.add_argument('--model')
    p.add_argument('--sans-outils', action='store_true', help='ne pas consulter la machine')
    p = sub.add_parser('ask', help='une question, sans historique')
    p.add_argument('question')
    p.add_argument('--model', help='imposer un modèle (par défaut, NEXUS choisit)')
    p.add_argument('--approfondi', action='store_true', help='prendre le meilleur modèle, quitte à attendre')
    p.add_argument('--sans-outils', action='store_true', help='ne pas consulter la machine')
    p = sub.add_parser('docs', help='bibliothèque de documents (indexer, liste, chercher, ranger, annuler, depenses)')
    p.add_argument('action', choices=['indexer', 'liste', 'chercher', 'ranger', 'annuler', 'depenses'])
    p.add_argument('texte', nargs='?', help='question (chercher) ou année (depenses)')
    p.add_argument('--dossier', help='dossier des documents (défaut : ~/Documents)')
    p.add_argument('--model', help='modèle de classement')
    p.add_argument('--categorie', help='catégorie (depenses)')
    p.add_argument('--appliquer', action='store_true', help='ranger pour de bon (ranger)')
    p.add_argument('--sans-classement', action='store_true', help='indexer sans classer (plus rapide)')
    p = sub.add_parser('taches', help='tâches planifiées (liste, activer, desactiver, lancer, horaire)')
    p.add_argument('action', nargs='?', default='liste', choices=['liste', 'activer', 'desactiver', 'lancer', 'horaire'])
    p.add_argument('valeur', nargs='?', help='identifiant de la tâche')
    p.add_argument('horaire', nargs='?', help='horaire JSON, ex. {"type": "quotidien", "heure": "19:30"}')
    p = sub.add_parser('notifications', help='notifications des tâches planifiées')
    p.add_argument('--toutes', action='store_true', help='aussi les notifications déjà lues')
    sub.add_parser('planificateur', help='service : exécute les tâches planifiées (boucle)')
    p = sub.add_parser('savoir-faire', help='savoir-faire (liste, voir, lancer, valider, supprimer)')
    p.add_argument('action', nargs='?', default='liste', choices=['liste', 'voir', 'lancer', 'valider', 'supprimer'])
    p.add_argument('valeur', nargs='?', help='identifiant du savoir-faire')
    p = sub.add_parser('souvenirs', help='mémoire à long terme (liste, ajouter, valider, refuser, oublier, tout-oublier)')
    p.add_argument('action', nargs='?', default='liste',
                   choices=['liste', 'ajouter', 'valider', 'refuser', 'oublier', 'tout-oublier'])
    p.add_argument('valeur', nargs='?', help='texte (ajouter) ou numéro')
    p = sub.add_parser('moteur', help='moteur colibrì tenu par NEXUS (etat, installer, demarrer, arreter, journal)')
    p.add_argument('action', nargs='?', default='etat', choices=['etat', 'installer', 'demarrer', 'arreter', 'journal'])
    p.add_argument('valeur', nargs='?', help='modèle à installer (défaut : celui que colibrì recommande pour la machine)')
    p = sub.add_parser('mise-a-jour', help='versions de NEXUS (etat, verifier, installer [VERSION], annuler)')
    p.add_argument('action', nargs='?', default='etat', choices=['etat', 'verifier', 'installer', 'annuler'])
    p.add_argument('valeur', nargs='?', help='installer : une version précise (0.28.1) plutôt que la dernière')
    p.add_argument('--oui', action='store_true', help='approuver sans question (à réserver aux scripts)')
    p.add_argument('--sans-tests', action='store_true', help='ne pas faire passer ses tests à la nouvelle version (déconseillé)')
    sub.add_parser('sante', help='ce que NEXUS constate sur lui-même (moteur, tâches, courrier, sauvegarde, place, service)')
    p = sub.add_parser('sauvegarde', help='sauvegarde chiffrée sur un autre disque (etat, installer DOSSIER, maintenant, verifier, restaurer DOSSIER, cle)')
    p.add_argument('action', nargs='?', default='etat', choices=['etat', 'installer', 'maintenant', 'verifier', 'restaurer', 'cle'])
    p.add_argument('valeur', nargs='?')
    p.add_argument('--depuis', help='restaurer : dossier du disque de sauvegarde (PC neuf)')
    p.add_argument('--reprendre', action='store_true', help='donner la clé d\'une sauvegarde existante')
    p.add_argument('--date', help='restaurer : la sauvegarde de ce jour-là (AAAA-MM-JJ) ou la dernière d\'avant')
    p = sub.add_parser('service', help='NEXUS démarre avec le PC, enfermé par systemd (etat, installer, retirer, journal)')
    p.add_argument('action', nargs='?', default='etat', choices=['etat', 'installer', 'retirer', 'journal'])
    p.add_argument('--vpn', action='store_true', help='écouter dans le tunnel privé seulement (défaut si le tunnel est préparé)')
    p.add_argument('--reseau', action='store_true', help='écouter sur le réseau de la maison')
    p.add_argument('--port', type=int, default=8765)
    p = sub.add_parser('alertes', help='alertes sur téléphone verrouillé, sans tiers (etat, activer, desactiver, renouveler, discret oui|non, essai)')
    p.add_argument('action', nargs='?', default='etat', choices=['etat', 'activer', 'desactiver', 'renouveler', 'discret', 'essai'])
    p.add_argument('valeur', nargs='?')
    p = sub.add_parser('vpn', help='tunnel privé WireGuard, sans tiers (etat, installer ADRESSE, ajouter NOM, retirer NOM)')
    p.add_argument('action', nargs='?', default='etat', choices=['etat', 'installer', 'ajouter', 'retirer'])
    p.add_argument('valeur', nargs='?', help='adresse publique de la maison (installer), ou nom de l\'appareil')
    p.add_argument('--port', type=int, default=51820, help='port UDP du tunnel (défaut 51820)')
    p = sub.add_parser('voix', help='parler à NEXUS depuis le téléphone (etat, installer [small|base], essai FICHIER)')
    p.add_argument('action', nargs='?', default='etat', choices=['etat', 'installer', 'essai'])
    p.add_argument('valeur', nargs='?', help='modèle à installer (small par défaut, base : plus léger), ou fichier audio à essayer')
    p = sub.add_parser('courrier', help='courrier en lecture seule (voir, ajouter gmail|laposte ADRESSE, retirer NOM, relever)')
    p.add_argument('action', nargs='?', default='voir', choices=['voir', 'ajouter', 'retirer', 'relever'])
    p.add_argument('valeur', nargs='?', help='gmail ou laposte (ajouter), nom de la boîte (retirer)')
    p.add_argument('adresse', nargs='?', help='adresse de la boîte à ajouter')
    p = sub.add_parser('journal', help='journal du matin (composer, liste, reglages)')
    p.add_argument('action', nargs='?', default='composer', choices=['composer', 'liste', 'reglages'])
    p.add_argument('valeur', nargs='?', help='réglage : imprimer, actualites, mot, imprimante')
    p.add_argument('choix', nargs='?', help='oui, non, ou le nom de l\'imprimante')
    p = sub.add_parser('activite', help='ce que NEXUS fait de lui-même (liste, annuler, valider, refuser, tournee, niveau)')
    p.add_argument('action', nargs='?', default='liste', choices=['liste', 'annuler', 'valider', 'refuser', 'tournee', 'niveau'])
    p.add_argument('valeur', nargs='?', help='numéro du geste, ou nom du geste pour « niveau »')
    p.add_argument('niveau', nargs='?', help='demander, prevenir, silence ou jamais')
    p = sub.add_parser('agenda', help='rendez-vous, rappels et choses à faire (voir, ajouter, fait)')
    p.add_argument('action', nargs='?', default='voir', choices=['voir', 'ajouter', 'fait'])
    p.add_argument('texte', nargs='?', help='ex. "Dentiste demain à 14h", "acheter du pain"')
    p.add_argument('--jours', type=int, default=14, help='nombre de jours affichés (défaut 14)')
    p = sub.add_parser('modeles', help='modèles locaux : ceux que NEXUS choisit, et pourquoi (liste, evaluer)')
    p.add_argument('action', nargs='?', default='liste', choices=['liste', 'evaluer'])
    p.add_argument('valeur', nargs='?', help='modèle à mesurer (défaut : tous)')
    p = sub.add_parser('serveur', help='serveur maison et appli téléphone (lancer, appairer, appareils, revoquer)')
    p.add_argument('action', nargs='?', default='lancer', choices=['lancer', 'appairer', 'appareils', 'revoquer'])
    p.add_argument('valeur', nargs='?', help='numéro de l\'appareil à révoquer')
    p.add_argument('--reseau', action='store_true', help='joignable depuis le réseau de la maison (chiffré)')
    p.add_argument('--vpn', action='store_true', help='joignable seulement par le tunnel privé (le plus sûr ; voir nexus vpn)')
    p.add_argument('--port', type=int, default=8765)
    p.add_argument('--sans-chiffrement', action='store_true', help='déconseillé : HTTP en clair sur le réseau')
    p.add_argument('--bavard', action='store_true', help='affiche chaque requête (sans son contenu)')
    sub.add_parser('outils', help='liste des outils de consultation')
    sub.add_parser('diagnostic', help='rapport complet de la machine (sans IA)')
    p = sub.add_parser('outil', help='lancer un outil de consultation à la main')
    p.add_argument('nom')
    p.add_argument('arguments', nargs='?', help='objet JSON, ex. {"critere": "processeur"}')
    p = sub.add_parser('watch', help='alertes au fil de l\'eau')
    p.add_argument('--interval', type=float, default=2.0)
    sub.add_parser('history', help='conversations enregistrées')
    sub.add_parser('forget', help='effacer toutes les conversations')
    args = parser.parse_args(argv)
    if getattr(args, 'interval', 2.0) < 0.5:
        parser.error('--interval doit valoir au moins 0.5')
    handlers = {'status': cmd_status, 'chat': cmd_chat, 'ask': cmd_ask, 'watch': cmd_watch,
                'history': cmd_history, 'forget': cmd_forget, 'outils': cmd_outils, 'docs': cmd_docs, 'souvenirs': cmd_souvenirs, 'savoir-faire': cmd_skills, 'taches': cmd_taches, 'notifications': cmd_notifications,
                'planificateur': cmd_planificateur, 'moteur': cmd_moteur, 'serveur': cmd_serveur, 'modeles': cmd_modeles, 'agenda': cmd_agenda, 'activite': cmd_activite, 'journal': cmd_journal, 'courrier': cmd_courrier, 'voix': cmd_voix, 'vpn': cmd_vpn, 'alertes': cmd_alertes, 'service': cmd_service, 'sauvegarde': cmd_sauvegarde, 'sante': cmd_sante, 'mise-a-jour': cmd_mise_a_jour, 'diagnostic': cmd_diagnostic, 'outil': cmd_outil, 'desktop': cmd_desktop, None: cmd_desktop}
    settings = Settings.load()
    if args.command not in (None, 'desktop'):
        from .openai_compat import ManagedColibriClient
        ManagedColibriClient.on_progress = staticmethod(_progress)
    try:
        return handlers[args.command](args, settings)
    except (OllamaError, ValueError, OSError) as exc:
        print(f'NEXUS : {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
