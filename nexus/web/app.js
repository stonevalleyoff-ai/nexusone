/* Appli téléphone de NEXUS. Aucune donnée n'est gardée ici : tout reste sur le PC de la maison. */
'use strict';

const $ = (id) => document.getElementById(id);
let conversation = null;      // numéro de la conversation affichée
let repond = false;           // une réponse est en train d'arriver sur cet appareil
let dernierEtat = null;

// -- échanges avec le PC ------------------------------------------------------------------
class NonAppaire extends Error {}

async function api(methode, chemin, corps) {
  const options = { method: methode, credentials: 'same-origin', headers: { 'X-Nexus': '1' } };
  if (corps !== undefined) {
    options.headers['Content-Type'] = 'application/json';
    options.body = JSON.stringify(corps);
  }
  let reponse;
  try {
    reponse = await fetch(chemin, options);
  } catch (e) {
    throw new Error('Le PC de la maison ne répond pas (réseau ?)');
  }
  if (reponse.status === 401) throw new NonAppaire();
  let donnees = {};
  try { donnees = await reponse.json(); } catch (e) { /* réponse vide */ }
  if (!reponse.ok) throw new Error(donnees.erreur || `Erreur ${reponse.status}`);
  return donnees;
}

function element(balise, classe, texte) {
  const e = document.createElement(balise);
  if (classe) e.className = classe;
  if (texte !== undefined) e.textContent = texte;
  return e;
}

// -- mise en forme : un Markdown minimal, construit en nœuds (jamais de HTML injecté) ------
function enLigne(parent, texte) {
  const motif = /\*\*([^*]+)\*\*|`([^`]+)`/g;
  let fin = 0, m;
  while ((m = motif.exec(texte)) !== null) {
    if (m.index > fin) parent.append(texte.slice(fin, m.index));
    parent.append(element(m[1] !== undefined ? 'strong' : 'code', '', m[1] !== undefined ? m[1] : m[2]));
    fin = motif.lastIndex;
  }
  if (fin < texte.length) parent.append(texte.slice(fin));
}

function miseEnForme(cible, texte) {
  cible.replaceChildren();
  let liste = null;
  for (const ligne of texte.split('\n')) {
    const puce = /^\s*(?:[-*•]|\d+[.)])\s+(.*)$/.exec(ligne);
    if (puce) {
      if (!liste) { liste = element('ul'); cible.append(liste); }
      const li = element('li'); enLigne(li, puce[1]); liste.append(li);
      continue;
    }
    liste = null;
    if (!ligne.trim()) continue;
    const titre = /^#+\s+(.*)$/.exec(ligne);
    const p = element('p');
    if (titre) p.append(element('strong', '', titre[1])); else enLigne(p, ligne);
    cible.append(p);
  }
}

// -- discussion ---------------------------------------------------------------------------
function ajouterMessage(qui, texte) {
  const bloc = element('div', 'message ' + (qui === 'user' ? 'vous' : 'nexus'));
  bloc.append(element('div', 'qui', qui === 'user' ? 'VOUS' : 'NEXUS'));
  if (qui !== 'user') bloc.append(element('div', 'consultations'));   // « consulte : … », avant la réponse
  const corps = element('div', 'texte');
  if (qui === 'user') corps.textContent = texte; else miseEnForme(corps, texte);
  bloc.append(corps);
  $('messages').append(bloc);
  bloc.scrollIntoView({ block: 'end' });
  return bloc;
}

function note(bloc, classe, texte) {
  const n = element('div', 'note ' + classe, texte);
  bloc.append(n);
  n.scrollIntoView({ block: 'end' });
  return n;
}

function accueil() {
  $('messages').replaceChildren();
  ajouterMessage('assistant', 'Bonjour. Je tourne sur le PC de la maison : tes questions et tes documents ' +
    'n\'en sortent pas. Je peux expliquer, rédiger, chercher dans tes documents et examiner le PC. ' +
    'Je ne fais que lire : je ne modifie jamais rien.');
}

async function ouvrirConversation(numero) {
  const { messages } = await api('GET', `/api/conversations/${numero}`);
  conversation = numero;
  $('messages').replaceChildren();
  for (const m of messages) ajouterMessage(m.role, m.content);
}

function proposerSouvenir(bloc, proposition) {
  const carte = element('div', 'proposition');
  carte.append(element('div', '', `Retenir « ${proposition.texte} » ?`));
  for (const [libelle, garder] of [['Oui, retiens-le', true], ['Non', false]]) {
    const bouton = element('button', garder ? 'principal' : '', libelle);
    bouton.type = 'button';
    bouton.addEventListener('click', async () => {
      try {
        await api('POST', `/api/souvenirs/${proposition.id}`, { garder });
        carte.replaceChildren(element('div', 'discret', garder ? 'Retenu.' : 'Pas retenu.'));
      } catch (e) { carte.append(element('div', 'erreur', e.message)); }
    });
    carte.append(bouton);
  }
  bloc.append(carte);
}

function modeReponse(actif) {
  repond = actif;
  $('envoyer').textContent = actif ? 'Arrêter' : 'Envoyer';
  $('question').disabled = actif;
}

async function poser(question) {
  ajouterMessage('user', question);
  const bloc = ajouterMessage('assistant', '');
  const corps = bloc.querySelector('.texte');
  const attente = note(bloc, '', 'NEXUS réfléchit…');
  let texte = '', termine = false;
  modeReponse(true);
  try {
    const reponse = await fetch('/api/question', {
      method: 'POST', credentials: 'same-origin',
      headers: { 'X-Nexus': '1', 'Content-Type': 'application/json' },
      body: JSON.stringify({ question, conversation, outils: $('outils').checked,
        approfondi: !$('option-approfondi').hidden && $('approfondi').checked }),
    });
    if (reponse.status === 401) throw new NonAppaire();
    if (!reponse.ok) {
      let d = {}; try { d = await reponse.json(); } catch (e) { /* vide */ }
      throw new Error(d.erreur || `Erreur ${reponse.status}`);
    }
    const lecteur = reponse.body.getReader();
    const decodeur = new TextDecoder();
    let tampon = '';
    for (;;) {
      const { value, done } = await lecteur.read();
      if (done) break;
      tampon += decodeur.decode(value, { stream: true });
      let coupe;
      while ((coupe = tampon.indexOf('\n')) >= 0) {
        const ligne = tampon.slice(0, coupe); tampon = tampon.slice(coupe + 1);
        if (!ligne.trim()) continue;
        const e = JSON.parse(ligne);
        if (e.t === 'debut') conversation = e.conversation;
        else if (e.t === 'etat') attente.textContent = e.v;
        else if (e.t === 'outil') note(bloc.querySelector('.consultations'), 'outil', '↳ consulte : ' + e.v);
        else if (e.t === 'texte') {
          attente.remove();
          texte += e.v; miseEnForme(corps, texte);
          bloc.scrollIntoView({ block: 'end' });
        } else if (e.t === 'erreur') { attente.remove(); note(bloc, 'souci', e.v); termine = true; }
        else if (e.t === 'fin') {
          termine = true; attente.remove();
          const details = [];
          if (e.interrompu) details.push('interrompu');
          if (e.jetons) details.push(`${e.jetons} jetons, ${e.vitesse}/s`);
          if (e.modele) details.push('modèle : ' + e.modele);
          if (e.sources && e.sources.length) details.push('Sources : ' + e.sources.join(' · '));
          if (e.souvenirs && e.souvenirs.length) details.push('Souvenirs utilisés : ' + e.souvenirs.join(' · '));
          if (details.length) note(bloc, '', details.join('  —  '));
          for (const p of e.propositions || []) proposerSouvenir(bloc, p);
          if (lireReponse) dire(texte);
        }
      }
    }
    if (!termine) throw new TypeError('coupure');
  } catch (e) {
    attente.remove();
    if (e instanceof NonAppaire) return afficherAppairage();
    if (e instanceof TypeError) {
      // Connexion coupée (écran éteint, réseau) : le PC termine et enregistre la réponse.
      note(bloc, '', 'Connexion coupée : le PC termine la réponse, elle réapparaîtra ici.');
      suivreReponseDistante();
    } else note(bloc, 'souci', e.message);
  } finally {
    lireReponse = false;
    modeReponse(false);
    rafraichirEtat();
  }
}

let suivi = null;
function suivreReponseDistante() {
  if (suivi) return;
  suivi = setInterval(async () => {
    try {
      const etat = await api('GET', '/api/etat');
      if (!etat.occupe) {
        clearInterval(suivi); suivi = null;
        if (conversation !== null && !repond) await ouvrirConversation(conversation);
      }
    } catch (e) { /* on réessaiera */ }
  }, 4000);
}

async function listerConversations() {
  const { conversations } = await api('GET', '/api/conversations');
  const liste = $('liste-conversations');
  liste.replaceChildren();
  if (!conversations.length) liste.append(element('li', 'discret', 'Aucune conversation enregistrée.'));
  for (const c of conversations) {
    const li = element('li');
    const ouvrir = element('button', 'ouvrir', c.title);
    ouvrir.addEventListener('click', async () => {
      $('conversations').hidden = true;
      montrerVue('discussion');
      try { await ouvrirConversation(c.id); } catch (e) { alert(e.message); }
    });
    const effacer = element('button', 'effacer', '🗑');
    effacer.setAttribute('aria-label', 'Effacer cette conversation');
    effacer.addEventListener('click', async () => {
      if (!confirm('Effacer définitivement cette conversation ?')) return;
      await api('DELETE', `/api/conversations/${c.id}`);
      if (conversation === c.id) { conversation = null; accueil(); }
      listerConversations();
    });
    li.append(ouvrir, effacer);
    liste.append(li);
  }
}

// -- voix : dicter la question, écouter la réponse -----------------------------------------
// Le son part vers le PC de la maison, qui le transcrit lui-même. La réponse est lue par la
// voix du téléphone. Rien ne passe par un service extérieur.
let enregistreur = null;
let lireReponse = false;       // la dernière question a été dictée : sa réponse sera lue à voix haute

function dire(texte) {
  if (!('speechSynthesis' in window) || !texte) return;
  window.speechSynthesis.cancel();
  const propre = texte.replace(/\*\*|`|^#+\s*/gm, '').replace(/^\s*(?:[-*•]|\d+[.)])\s+/gm, '').replace(/\s+/g, ' ').slice(0, 1500);
  const parole = new SpeechSynthesisUtterance(propre);
  parole.lang = 'fr-FR';
  window.speechSynthesis.speak(parole);
}

async function basculerMicro() {
  const bouton = $('micro');
  if ('speechSynthesis' in window) window.speechSynthesis.cancel();      // un appui coupe aussi la lecture en cours
  if (enregistreur) { enregistreur.stop(); return; }
  if (repond) return;
  let flux;
  try {
    flux = await navigator.mediaDevices.getUserMedia({ audio: true });
  } catch (e) {
    ajouterMessage('assistant', 'Je n\'ai pas accès au micro : autorise-le pour cette page dans les réglages du navigateur.');
    return;
  }
  const morceaux = [];
  enregistreur = new MediaRecorder(flux);
  enregistreur.addEventListener('dataavailable', (e) => { if (e.data.size) morceaux.push(e.data); });
  enregistreur.addEventListener('stop', async () => {
    for (const piste of flux.getTracks()) piste.stop();
    const type = enregistreur.mimeType || 'audio/webm';
    enregistreur = null;
    bouton.classList.remove('ecoute'); bouton.textContent = '…'; bouton.disabled = true;
    try {
      const reponse = await fetch('/api/voix', { method: 'POST', credentials: 'same-origin',
        headers: { 'X-Nexus': '1', 'Content-Type': type }, body: new Blob(morceaux, { type }) });
      let d = {}; try { d = await reponse.json(); } catch (e) { /* vide */ }
      if (reponse.status === 401) return afficherAppairage();
      if (!reponse.ok) throw new Error(d.erreur || `Erreur ${reponse.status}`);
      if (!d.texte) throw new Error('Je n\'ai rien entendu de clair. Réessaie, plus près du micro.');
      lireReponse = true;
      poser(d.texte);
    } catch (e) {
      ajouterMessage('assistant', e instanceof TypeError ? 'Le PC de la maison ne répond pas (réseau ?)' : e.message);
    } finally {
      bouton.textContent = '🎤'; bouton.disabled = false;
    }
  });
  enregistreur.start();
  bouton.classList.add('ecoute'); bouton.textContent = '■';
  setTimeout(() => { if (enregistreur && enregistreur.state === 'recording') enregistreur.stop(); }, 60000);   // une minute au plus
}

// -- documents ----------------------------------------------------------------------------
function carteDocument(d, extrait) {
  const carte = element(d.id ? 'a' : 'div', 'carte');
  if (d.id) { carte.href = `/api/documents/${d.id}/fichier`; carte.setAttribute('download', ''); }
  const haut = element('div', 'haut');
  haut.append(element('span', 'nom', d.titre), element('span', 'discret', d.montant || ''));
  carte.append(haut);
  const details = [d.date, d.categorie, d.emetteur, d.page ? `page ${d.page}` : '',
    d.reconnu ? 'texte reconnu (scan)' : '', d.lisible === false ? 'pas de texte lisible' : ''].filter(Boolean).join(' · ');
  carte.append(element('div', 'discret', details || d.chemin));
  if (extrait) carte.append(element('div', 'extrait', extrait));
  return carte;
}

async function chargerDocuments(recherche) {
  const zone = $('resultats');
  zone.replaceChildren(element('p', 'discret', recherche ? 'Recherche…' : 'Chargement…'));
  try {
    const r = await api('GET', '/api/documents' + (recherche ? '?q=' + encodeURIComponent(recherche) : ''));
    zone.replaceChildren();
    if (r.passages) {
      if (r.par_mots) zone.append(element('p', 'discret', 'Recherche par mots seulement (le modèle de sens ne répond pas).'));
      if (!r.passages.length) zone.append(element('p', 'discret', 'Rien trouvé.'));
      for (const p of r.passages) zone.append(carteDocument(p, p.extrait));
    } else {
      if (!r.documents.length) zone.append(element('p', 'discret', 'Aucun document pour l\'instant.'));
      for (const d of r.documents) zone.append(carteDocument(d));
    }
  } catch (e) {
    if (e instanceof NonAppaire) return afficherAppairage();
    zone.replaceChildren(element('p', 'erreur', e.message));
  }
}

async function envoyerFichier(fichier) {
  const etat = $('etat-envoi');
  etat.className = 'discret';
  etat.textContent = `Envoi de ${fichier.name}…`;
  try {
    const reponse = await fetch('/api/documents?nom=' + encodeURIComponent(fichier.name), {
      method: 'POST', credentials: 'same-origin', headers: { 'X-Nexus': '1' }, body: fichier,
    });
    let d = {}; try { d = await reponse.json(); } catch (e) { /* vide */ }
    if (reponse.status === 401) return afficherAppairage();
    if (!reponse.ok) throw new Error(d.erreur || `Erreur ${reponse.status}`);
    etat.textContent = `Reçu : ${d.chemin}. NEXUS le lit et le classe.`;
    setTimeout(() => { chargerDocuments(''); chargerRangement(); }, 2500);
    setTimeout(() => { chargerRangement(); rafraichirEtat(); }, 15000);
  } catch (e) {
    etat.className = 'erreur';
    etat.textContent = e instanceof TypeError ? 'Envoi impossible (réseau ?)' : e.message;
  }
}

// -- rangement des documents reçus : NEXUS propose, la personne décide ---------------------
let rangementOuvert = false;
let messageRangement = '';

function bouton(libelle, classe, action) {
  const b = element('button', classe, libelle);
  b.type = 'button';
  b.addEventListener('click', async () => {
    b.disabled = true;
    try { await action(); } catch (e) {
      if (e instanceof NonAppaire) return afficherAppairage();
      messageRangement = e.message;
      chargerRangement();
    }
  });
  return b;
}

function formulaireCorrection(carte, p, categories) {
  const form = element('form', 'correction');
  const champ = (libelle, noeud) => { form.append(element('label', '', libelle), noeud); return noeud; };
  const categorie = champ('Catégorie', element('select'));
  for (const [cle, nom] of Object.entries(categories)) {
    const option = element('option', '', nom); option.value = cle; option.selected = cle === p.cle_categorie;
    categorie.append(option);
  }
  const titre = champ('Titre', element('input')); titre.value = p.titre || ''; titre.maxLength = 90;
  const emetteur = champ('Émetteur (organisme, magasin…)', element('input')); emetteur.value = p.emetteur || ''; emetteur.maxLength = 60;
  const date = champ('Date du document', element('input')); date.type = 'date'; date.value = p.date || '';
  const actions = element('div', 'actions');
  const valider = element('button', 'principal', 'Enregistrer'); valider.type = 'submit';
  const annuler = element('button', '', 'Annuler'); annuler.type = 'button';
  annuler.addEventListener('click', () => chargerRangement());
  actions.append(valider, annuler);
  form.append(actions);
  form.addEventListener('submit', async (evenement) => {
    evenement.preventDefault();
    try {
      await api('POST', `/api/documents/${p.id}`,
        { categorie: categorie.value, titre: titre.value, emetteur: emetteur.value, date: date.value });
      messageRangement = 'Correction enregistrée.';
    } catch (e) {
      if (e instanceof NonAppaire) return afficherAppairage();
      messageRangement = e.message;
    }
    chargerRangement();
  });
  carte.querySelector('.actions').replaceWith(form);
}

async function chargerRangement() {
  const zone = $('rangement');
  let r;
  try { r = await api('GET', '/api/rangement'); } catch (e) {
    if (e instanceof NonAppaire) afficherAppairage();
    zone.hidden = true;
    return;
  }
  const prets = r.propositions.filter((p) => p.propose);
  zone.replaceChildren();
  zone.hidden = !r.propositions.length && !r.dernier && !messageRangement;
  if (zone.hidden) return;
  const tete = element('div', 'carte tete');
  const nombre = r.propositions.length;
  tete.append(element('div', 'nom', nombre ? `${nombre} document${nombre > 1 ? 's' : ''} reçu${nombre > 1 ? 's' : ''} à ranger` : 'Rien à ranger'));
  if (nombre) tete.append(element('div', 'discret', 'NEXUS propose, tu décides. Rien n\'est déplacé sans ton accord.'));
  if (r.en_lecture) tete.append(element('div', 'discret', `${r.en_lecture} document(s) encore en cours de lecture…`));
  if (messageRangement) tete.append(element('div', 'chemin propose', messageRangement));
  const actions = element('div', 'actions');
  if (nombre) actions.append(bouton(rangementOuvert ? 'Masquer' : 'Voir les propositions', rangementOuvert ? '' : 'principal',
    async () => { rangementOuvert = !rangementOuvert; messageRangement = ''; await chargerRangement(); }));
  if (rangementOuvert && prets.length > 1) actions.append(bouton(`Tout ranger (${prets.length})`, 'principal', async () => {
    const fait = await api('POST', '/api/rangement', { ids: prets.map((p) => p.id) });
    messageRangement = `${fait.ranges.length} document(s) rangé(s).`;
    await apresRangement();
  }));
  if (r.dernier) actions.append(bouton(`Annuler le dernier rangement (${r.dernier.nombre})`, '', async () => {
    const fait = await api('POST', '/api/rangement/annuler', {});
    messageRangement = `${fait.annules.length} déplacement(s) annulé(s).` +
      (fait.non_annules.length ? ` ${fait.non_annules.length} impossible(s) à annuler (fichier déplacé ou supprimé depuis).` : '');
    await apresRangement();
  }));
  tete.append(actions);
  zone.append(tete);
  if (!rangementOuvert) return;
  for (const p of r.propositions) {
    const carte = element('div', 'carte');
    carte.append(element('div', 'nom', p.titre));
    const details = [p.date, p.categorie, p.emetteur, p.montant, p.reconnu ? 'texte reconnu (scan)' : '',
      p.lisible === false ? 'pas de texte lisible' : ''].filter(Boolean).join(' · ');
    if (details) carte.append(element('div', 'discret', details));
    carte.append(element('div', 'chemin', 'Ici : ' + p.chemin));
    carte.append(element('div', 'chemin propose', p.propose ? 'Proposé : ' + p.propose : 'Pas encore classé : à corriger à la main.'));
    const choix = element('div', 'actions');
    if (p.propose) choix.append(bouton('Ranger', 'principal', async () => {
      const fait = await api('POST', '/api/rangement', { ids: [p.id] });
      messageRangement = fait.ranges.length ? `Rangé : ${fait.ranges[0].vers}` : 'Ce document n\'a pas pu être rangé.';
      await apresRangement();
    }));
    choix.append(bouton('Corriger', '', async () => formulaireCorrection(carte, p, r.categories)));
    choix.append(bouton('Laisser ici', '', async () => {
      await api('POST', `/api/documents/${p.id}/laisser`, {});
      messageRangement = 'Laissé à sa place.';
      await apresRangement();
    }));
    carte.append(choix);
    zone.append(carte);
  }
}

async function apresRangement() {
  await chargerRangement();
  await chargerDocuments($('recherche').value.trim());
  rafraichirEtat();
}

// -- agenda : rendez-vous, rappels, choses à faire ------------------------------------------
async function chargerAgenda() {
  let a;
  try { a = await api('GET', '/api/agenda'); } catch (e) {
    if (e instanceof NonAppaire) return afficherAppairage();
    $('evenements').replaceChildren(element('p', 'erreur', e.message));
    return;
  }
  const agir = (noeud, action) => noeud.addEventListener('click', async () => {
    try { await action(); } catch (e) { $('message-agenda').textContent = e.message; }
    chargerAgenda();
  });
  const zone = $('evenements');
  zone.replaceChildren();
  if (!a.evenements.length) zone.append(element('p', 'discret', 'Rien de prévu dans les 30 jours.'));
  let jour = null;
  for (const e of a.evenements) {
    if (e.jour !== jour) { jour = e.jour; zone.append(element('div', 'jour-agenda', jour)); }
    const ligne = element('div', 'ligne-agenda');
    ligne.append(element('span', 'heure', e.heure || 'journée'));
    const corps = element('div', 'corps');
    corps.append(element('div', '', e.titre));
    const details = [e.lieu, e.repetition, e.notes].filter(Boolean).join(' · ');
    if (details) corps.append(element('div', 'discret', details));
    const retirer = element('button', '', '✕');
    retirer.setAttribute('aria-label', 'Supprimer ce rendez-vous');
    agir(retirer, async () => {
      if (confirm(`Supprimer « ${e.titre} »${e.repetition ? ' et toutes ses répétitions' : ''} ?`)) await api('DELETE', `/api/agenda/evenements/${e.id}`);
    });
    ligne.append(corps, retirer);
    zone.append(ligne);
  }
  const liste = $('taches');
  liste.replaceChildren();
  if (!a.taches.length) liste.append(element('p', 'discret', 'Rien à faire. Profite.'));
  for (const t of [...a.taches, ...a.faites]) {
    const ligne = element('div', 'ligne-agenda' + (t.faite ? ' faite' : ''));
    const cocher = element('button', t.faite ? '' : 'principal', t.faite ? '↺' : '✓');
    cocher.setAttribute('aria-label', t.faite ? 'Remettre dans la liste' : 'Marquer comme fait');
    agir(cocher, () => api('POST', `/api/agenda/taches/${t.id}`, { faite: !t.faite }));
    const corps = element('div', 'corps');
    corps.append(element('div', '', t.texte));
    if (t.pour) corps.append(element('div', t.en_retard ? 'retard' : 'discret', (t.en_retard ? 'en retard : ' : 'pour ') + t.pour));
    const retirer = element('button', '', '✕');
    retirer.setAttribute('aria-label', 'Supprimer');
    agir(retirer, () => api('DELETE', `/api/agenda/taches/${t.id}`));
    ligne.append(cocher, corps, retirer);
    liste.append(ligne);
  }
}

$('form-agenda').addEventListener('submit', async (evenement) => {
  evenement.preventDefault();
  const phrase = $('phrase-agenda').value.trim();
  if (!phrase) return;
  try {
    const r = await api('POST', '/api/agenda', { phrase });
    $('message-agenda').textContent = r.message;
    $('phrase-agenda').value = '';
  } catch (e) {
    if (e instanceof NonAppaire) return afficherAppairage();
    $('message-agenda').textContent = e.message;
  }
  chargerAgenda();
});

// -- maison -------------------------------------------------------------------------------
async function chargerMaison() {
  await rafraichirEtat();
  chargerSante();
  chargerJournal();
  chargerRelais();
  chargerCourrier();
  chargerActivite();
  try {
    const { notifications } = await api('GET', '/api/notifications');
    const zone = $('notifications');
    zone.replaceChildren();
    if (!notifications.length) zone.append(element('p', 'discret', 'Aucune notification.'));
    for (const n of notifications) {
      const carte = element('div', 'carte' + (n.lue ? '' : ' non-lue'));
      const haut = element('div', 'haut');
      haut.append(element('span', 'nom', n.titre), element('span', 'discret', (n.creee || '').replace('T', ' ')));
      const texte = element('div', 'rapport');
      miseEnForme(texte, n.texte);
      carte.append(haut, texte);
      zone.append(carte);
    }
    if (notifications.some((n) => !n.lue)) { await api('POST', '/api/notifications/lues', {}); $('pastille').hidden = true; }
  } catch (e) { if (e instanceof NonAppaire) afficherAppairage(); }
}

async function chargerJournal(message) {
  let j;
  try { j = await api('GET', '/api/journal'); } catch (e) {
    if (e instanceof NonAppaire) afficherAppairage();
    return;
  }
  const zone = $('journal');
  zone.replaceChildren();
  const carte = element('div', 'carte');
  const dernier = j.numeros[0];
  carte.append(element('div', 'nom', dernier ? dernier.titre : 'Pas encore de numéro'));
  carte.append(element('div', 'discret', message || 'Composé chaque matin à 6h30 : agenda, choses à faire, électricité, ce que NEXUS a fait, actualités choisies pour toi.'));
  const actions = element('div', 'actions');
  const lien = (libelle, adresse, classe) => {
    const a = element('a', 'bouton' + (classe ? ' ' + classe : ''), libelle);
    a.href = adresse; a.target = '_blank'; a.rel = 'noopener';
    return a;
  };
  if (dernier) {
    actions.append(lien('Lire', `/api/journal/${dernier.jour}`));
    if (dernier.pdf) actions.append(lien('PDF', `/api/journal/${dernier.jour}/pdf`, 'secondaire'));
  }
  const composer = element('button', '', dernier ? 'Refaire maintenant' : 'Composer maintenant');
  composer.type = 'button';
  composer.addEventListener('click', async () => {
    composer.disabled = true; composer.textContent = 'Composition… (une minute)';
    try {
      const r = await api('POST', '/api/journal', {});
      chargerJournal(r.notes.length ? r.notes.join(' ') : (r.imprime ? 'Numéro composé et envoyé à l\'imprimante.' : 'Numéro composé.'));
    } catch (e) { chargerJournal(e.message); }
  });
  actions.append(composer);
  carte.append(actions);
  const imprimer = element('label', 'option');
  const case_ = element('input'); case_.type = 'checkbox'; case_.checked = j.reglages.imprimer;
  case_.addEventListener('change', async () => {
    try { await api('POST', '/api/journal/reglages', { imprimer: case_.checked }); } catch (e) { alert(e.message); }
  });
  imprimer.append(case_, ' L\'imprimer chaque matin');
  carte.append(imprimer);
  if (j.numeros.length > 1) {
    const anciens = element('div', 'discret');
    anciens.append('Numéros précédents : ');
    for (const n of j.numeros.slice(1, 8)) {
      const a = element('a', '', n.jour.slice(8) + '/' + n.jour.slice(5, 7));
      a.href = `/api/journal/${n.jour}`; a.target = '_blank'; a.rel = 'noopener';
      anciens.append(a, ' ');
    }
    carte.append(anciens);
  }
  zone.append(carte);
}

// Ce que NEXUS constate sur lui-même (moteur, tâches de nuit, courrier, sauvegarde, place) : des faits, pas un modèle.
async function chargerSante() {
  let s;
  try { s = await api('GET', '/api/sante'); } catch (e) {
    if (e instanceof NonAppaire) afficherAppairage();
    return;
  }
  const zone = $('sante');
  zone.replaceChildren();
  const mots = { critical: 'Grave', warning: 'À régler', info: 'Conseil' };
  const serieux = s.constats.filter((c) => c.niveau !== 'info');
  const carte = element('div', 'carte' + (serieux.length ? ' non-lue' : ''));
  carte.append(element('div', 'nom', serieux.length ? `${serieux.length} chose${serieux.length > 1 ? 's' : ''} à régler` : 'Tout va bien'));
  for (const c of s.constats) {
    const ligne = element('div', 'extrait');
    ligne.append(element('strong', '', mots[c.niveau] + ' : '), c.titre);
    carte.append(ligne, element('div', 'discret', c.conseil));
  }
  for (const b of s.bien) carte.append(element('div', 'discret', b));
  zone.append(carte);
}

// Alertes sur téléphone verrouillé : l'appli libre ntfy garde une connexion avec le PC, sans passer par Google.
async function chargerRelais(message) {
  let r;
  try { r = await api('GET', '/api/alertes'); } catch (e) {
    if (e instanceof NonAppaire) afficherAppairage();
    return;
  }
  const zone = $('relais');
  zone.replaceChildren();
  if (!r.disponible) return;
  const carte = element('div', 'carte');
  const changer = async (corps, texte) => {
    try { await api('POST', '/api/alertes', corps); chargerRelais(texte); } catch (e) { chargerRelais(e.message); }
  };
  const action = (libelle, classe, faire) => {
    const b = element('button', classe, libelle);
    b.type = 'button';
    b.addEventListener('click', () => { b.disabled = true; faire(); });
    return b;
  };
  const actions = element('div', 'actions');
  if (!r.active) {
    carte.append(element('div', 'nom', 'Éteintes'));
    carte.append(element('div', 'discret', message || 'Rappels, courrier important, gestes de NEXUS : reçus même écran éteint, grâce à l\'appli libre ntfy reliée directement au PC. Rien ne passe par Google ni par un autre service.'));
    actions.append(action('Allumer', '', () => changer({ active: true })));
    carte.append(actions);
    zone.append(carte);
    return;
  }
  carte.append(element('div', 'nom', r.relies ? 'Allumées : un téléphone écoute' : 'Allumées : aucun téléphone ne les écoute encore'));
  if (message) carte.append(element('div', 'discret', message));
  if (!r.relies) {      // une fois le téléphone relié, le mode d'emploi ne sert plus
    const etapes = element('ol', 'discret');
    etapes.append(element('li', '', 'Installer l\'appli ntfy (boutique F-Droid, ou Play Store).'));
    etapes.append(element('li', '', 'Appuyer sur « Relier l\'appli ntfy » ci-dessous, puis accepter l\'abonnement.'));
    etapes.append(element('li', '', 'Appuyer sur « Envoyer un essai », verrouiller le téléphone : l\'alerte doit arriver.'));
    carte.append(etapes);
  }
  const relier = element('a', 'bouton', 'Relier l\'appli ntfy');
  relier.href = r.lien;
  actions.append(relier);
  actions.append(action('Envoyer un essai', 'secondaire', async () => {
    try { await api('POST', '/api/alertes/essai', {}); chargerRelais('Essai envoyé.'); } catch (e) { chargerRelais(e.message); }
  }));
  carte.append(actions);
  const manuel = element('details', 'discret');
  manuel.append(element('summary', '', 'Relier à la main'));
  manuel.append(element('div', '', 'Dans ntfy : « + », cocher « Utiliser un autre serveur », puis recopier :'));
  manuel.append(element('div', '', 'Serveur : ' + r.serveur));
  manuel.append(element('div', '', 'Sujet : ' + r.sujet));
  carte.append(manuel);
  const discret = element('label', 'option');
  const case_ = element('input'); case_.type = 'checkbox'; case_.checked = r.discret;
  case_.addEventListener('change', () => changer({ discret: case_.checked }));
  discret.append(case_, ' Mode discret : ne pas afficher le contenu sur l\'écran verrouillé');
  carte.append(discret);
  const fin = element('div', 'actions');
  fin.append(action('Changer la clé', 'secondaire', () => {
    if (confirm('Les téléphones déjà reliés ne recevront plus rien et devront être reliés à nouveau. Continuer ?')) changer({ renouveler: true }, 'Clé changée : relier à nouveau le téléphone.');
    else chargerRelais();
  }));
  fin.append(action('Éteindre', 'secondaire', () => changer({ active: false })));
  carte.append(fin);
  zone.append(carte);
}

async function chargerCourrier() {
  let c;
  try { c = await api('GET', '/api/courrier'); } catch (e) {
    if (e instanceof NonAppaire) afficherAppairage();
    return;
  }
  const zone = $('courrier');
  zone.replaceChildren();
  if (!c.boites.length) {
    zone.append(element('p', 'discret', 'Aucune boîte mail renseignée. Sur le PC : nexus courrier ajouter gmail ton.adresse@gmail.com'));
    return;
  }
  if (!c.importants.length) zone.append(element('p', 'discret', `Rien d'important ces trois derniers jours (${c.boites.join(', ')} ; relève chaque nuit, en lecture seule).`));
  for (const m of c.importants) {
    const carte = element('div', 'carte');
    carte.append(element('div', 'nom', m.objet));
    if (m.resume) carte.append(element('div', 'extrait', m.resume));
    carte.append(element('div', 'discret', [m.de, m.boite, (m.recu || '').replace('T', ' à ')].filter(Boolean).join(' · ')));
    zone.append(carte);
  }
}

async function chargerActivite() {
  let a;
  try { a = await api('GET', '/api/activite'); } catch (e) {
    if (e instanceof NonAppaire) afficherAppairage();
    return;
  }
  const agir = (libelle, classe, chemin) => {
    const b = element('button', classe, libelle);
    b.type = 'button';
    b.addEventListener('click', async () => {
      b.disabled = true;
      try { const r = await api('POST', chemin, {}); if (r.message) alert(r.message); } catch (e) { alert(e.message); }
      chargerActivite(); rafraichirEtat();
    });
    return b;
  };
  const zone = $('activite');
  zone.replaceChildren();
  if (!a.actes.length) zone.append(element('p', 'discret', 'Rien pour l\'instant.'));
  const etats = { fait: '', propose: 'Proposition', annule: 'Annulé', refuse: 'Écarté' };
  for (const acte of a.actes) {
    const carte = element('div', 'carte' + (acte.etat === 'propose' ? ' non-lue' : ''));
    carte.append(element('div', acte.etat === 'annule' || acte.etat === 'refuse' ? 'discret' : 'nom', acte.resume));
    carte.append(element('div', 'discret', [etats[acte.etat], (acte.quand || '').replace('T', ' à ')].filter(Boolean).join(' · ')));
    const actions = element('div', 'actions');
    if (acte.etat === 'fait') actions.append(agir('Annuler', '', `/api/activite/${acte.id}/annuler`));
    if (acte.etat === 'propose') actions.append(agir('Oui, fais-le', 'principal', `/api/activite/${acte.id}/valider`),
      agir('Non', '', `/api/activite/${acte.id}/refuser`));
    if (actions.children.length) carte.append(actions);
    zone.append(carte);
  }
  const reglages = $('autonomie');
  reglages.replaceChildren();
  for (const [geste, libelle] of Object.entries(a.gestes)) {
    const bloc = element('div', 'correction');
    bloc.append(element('label', '', libelle));
    const choix = element('select');
    for (const [niveau, nom] of Object.entries(a.choix)) {
      const option = element('option', '', nom); option.value = niveau; option.selected = a.niveaux[geste] === niveau;
      choix.append(option);
    }
    choix.addEventListener('change', async () => {
      try { await api('POST', '/api/autonomie', { geste, niveau: choix.value }); } catch (e) { alert(e.message); chargerActivite(); }
    });
    bloc.append(choix);
    reglages.append(bloc);
  }
}

async function rafraichirEtat() {
  let etat;
  try {
    etat = await api('GET', '/api/etat');
  } catch (e) {
    if (e instanceof NonAppaire) { afficherAppairage(); return null; }
    $('voyant').className = 'mauvais';
    $('etat-texte').textContent = e.message;
    return null;
  }
  dernierEtat = etat;
  const voyant = $('voyant');
  if (etat.etat) { voyant.className = 'attention'; $('etat-texte').textContent = etat.etat; }
  else if (etat.occupe) { voyant.className = 'attention'; $('etat-texte').textContent = 'répond à une question…'; }
  else if (!etat.modele) { voyant.className = 'mauvais'; $('etat-texte').textContent = 'Moteur injoignable (voir Maison)'; }
  else { voyant.className = 'bon'; $('etat-texte').textContent = 'Prêt · ' + etat.modele; }
  $('micro').hidden = !(etat.voix && navigator.mediaDevices && window.MediaRecorder);
  $('option-approfondi').hidden = !etat.modele_approfondi;
  if (etat.modele_approfondi) $('texte-approfondi').textContent = `Réponse approfondie (${etat.modele_approfondi}, plus lent)`;
  const pastille = $('pastille');
  pastille.hidden = !etat.notifications;
  pastille.textContent = etat.notifications;
  $('pastille-docs').hidden = !etat.a_ranger;
  $('pastille-docs').textContent = etat.a_ranger;
  const machine = $('machine');
  machine.replaceChildren(...etat.machine.map((l) => element('li', '', l)));
  if (etat.documents) {
    let ligne = `Documents : ${etat.documents.nombre}`;
    if (etat.documents.en_lecture) ligne += ` (${etat.documents.en_lecture} en cours de lecture)`;
    machine.append(element('li', '', ligne));
  }
  const alertes = $('alertes');
  alertes.replaceChildren(...(etat.alertes.length ? etat.alertes.map((a) => element('li', 'alerte', a))
    : [element('li', 'discret', 'Aucune alerte')]));
  $('pied').textContent = `NEXUS ${etat.version} · ${etat.moteur} · appareil « ${etat.appareil} »`;
  return etat;
}

// -- navigation ---------------------------------------------------------------------------
function montrerVue(nom) {
  for (const vue of ['discussion', 'documents', 'agenda', 'maison']) $('vue-' + vue).hidden = vue !== nom;
  for (const bouton of document.querySelectorAll('nav button')) bouton.classList.toggle('actif', bouton.dataset.vue === nom);
  if (nom === 'documents') { chargerDocuments($('recherche').value.trim()); chargerRangement(); }
  if (nom === 'agenda') chargerAgenda();
  if (nom === 'maison') chargerMaison();
}

function afficherAppairage() {
  $('appli').hidden = true;
  $('appairage').hidden = false;
  $('code').focus();
}

async function demarrer() {
  const etat = await rafraichirEtat();
  if (!etat && !$('appairage').hidden) return;
  $('appairage').hidden = true;
  $('appli').hidden = false;
  accueil();
  if (etat && etat.occupe && etat.conversation_en_cours) {     // une réponse était en cours : on la retrouve
    try { await ouvrirConversation(etat.conversation_en_cours); } catch (e) { /* tant pis */ }
    note($('messages'), '', 'NEXUS termine une réponse…');
    suivreReponseDistante();
  }
}

$('form-appairage').addEventListener('submit', async (evenement) => {
  evenement.preventDefault();
  $('erreur-appairage').textContent = '';
  try {
    await api('POST', '/api/appairage', { code: $('code').value, nom: $('nom').value });
    $('code').value = '';
    await demarrer();
  } catch (e) {
    $('erreur-appairage').textContent = e instanceof NonAppaire ? 'Code refusé.' : e.message;
  }
});

$('code').addEventListener('input', () => {
  const chiffres = $('code').value.replace(/\D/g, '').slice(0, 8);
  $('code').value = chiffres.length > 4 ? chiffres.slice(0, 4) + '-' + chiffres.slice(4) : chiffres;
});

$('form-question').addEventListener('submit', async (evenement) => {
  evenement.preventDefault();
  if (repond) {
    try { await api('POST', '/api/arret', {}); } catch (e) { /* la réponse s'arrêtera d'elle-même */ }
    return;
  }
  const question = $('question').value.trim();
  if (!question) return;
  $('question').value = '';
  $('question').style.height = '';
  poser(question);
});

$('micro').addEventListener('click', basculerMicro);

$('question').addEventListener('input', () => {
  const zone = $('question');
  zone.style.height = 'auto';
  zone.style.height = Math.min(zone.scrollHeight, window.innerHeight * 0.4) + 'px';
});

$('question').addEventListener('keydown', (evenement) => {
  if (evenement.key === 'Enter' && !evenement.shiftKey && !evenement.isComposing && window.matchMedia('(pointer: fine)').matches) {
    evenement.preventDefault();
    $('form-question').requestSubmit();
  }
});

$('form-recherche').addEventListener('submit', (evenement) => {
  evenement.preventDefault();
  chargerDocuments($('recherche').value.trim());
});

$('fichier').addEventListener('change', () => {
  const fichier = $('fichier').files[0];
  if (fichier) envoyerFichier(fichier);
  $('fichier').value = '';
});

$('ouvrir-conversations').addEventListener('click', async () => {
  $('conversations').hidden = false;
  try { await listerConversations(); } catch (e) { if (e instanceof NonAppaire) afficherAppairage(); }
});
$('fermer-conversations').addEventListener('click', () => { $('conversations').hidden = true; });
$('conversations').addEventListener('click', (evenement) => {
  if (evenement.target === $('conversations')) $('conversations').hidden = true;
});
$('nouvelle').addEventListener('click', () => { if (!repond) { conversation = null; accueil(); montrerVue('discussion'); } });
for (const bouton of document.querySelectorAll('nav button')) {
  bouton.addEventListener('click', () => montrerVue(bouton.dataset.vue));
}

setInterval(() => { if (!document.hidden && !$('appli').hidden) rafraichirEtat(); }, 10000);
document.addEventListener('visibilitychange', () => { if (!document.hidden && !$('appli').hidden) rafraichirEtat(); });
demarrer();
