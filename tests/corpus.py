"""Jeu de documents de test : faux papiers français, avec la « bonne réponse » de chacun.

Toutes les entreprises et personnes sont fictives. Les noms de fichiers imitent de vrais
téléchargements (en vrac) pour tester le tri.

    python3 -m tests.corpus DOSSIER   # écrit les PDF et manifest.json
"""
from __future__ import annotations

import json
from pathlib import Path
import sys

from tests.pdfgen import write_pdf

ME = ['Camille Durand', '12 rue des Lilas', '74000 Annecy']


def _invoice(title, issuer, lines, total, date, ref):
    return [[f'# {issuer}', title, f'Référence : {ref}', f'Date : {date}', '', *ME, '', *lines, '',
             f'Montant total TTC : {total}', '', f'{issuer} - document fictif de test']]


DOCS: list[dict] = []


def doc(file, category, issuer, date, amount, pages, kind):
    DOCS.append({'file': file, 'category': category, 'issuer': issuer, 'date': date,
                 'amount': amount, 'kind': kind, 'pages': pages})


# --- Énergie : factures Tempo -------------------------------------------------------
for file, month, date, blue, white, red, total in [
        ('facture_2026_01_88213.pdf', 'janvier 2026', '05/02/2026', 610, 180, 95, '214,37 €'),
        ('facture_2026_02_88214.pdf', 'février 2026', '05/03/2026', 540, 205, 160, '268,92 €'),
        ('Facture-mars.pdf', 'mars 2026', '06/04/2026', 590, 120, 0, '131,05 €'),
        ('document (3).pdf', 'octobre 2025', '05/11/2025', 380, 0, 0, '72,48 €')]:
    doc(file, 'energie', 'Voltéa Énergie', date, total, _invoice(
        f'Facture d\'électricité - offre Tempo - période de {month}', 'Voltéa Énergie',
        ['Compteur Linky n° 21 45 87 63 90 12 - puissance souscrite 9 kVA',
         f'Jours bleus : {blue} kWh', f'Jours blancs : {white} kWh', f'Jours rouges : {red} kWh',
         f'Consommation totale : {blue + white + red} kWh',
         'Abonnement mensuel Tempo 9 kVA : 16,90 €'], total, date, file[:-4]), 'facture')
doc('conditions_tempo.pdf', 'energie', 'Voltéa Énergie', '12/09/2025', None, [
    ['# Voltéa Énergie', 'Conditions particulières de vente - Offre Tempo', 'Date : 12/09/2025', *ME, '',
     'Le contrat Tempo comporte 300 jours bleus, 43 jours blancs et 22 jours rouges par an.',
     'Les jours rouges sont placés entre le 1er novembre et le 31 mars, hors week-end.',
     'Heures creuses : de 22h00 à 6h00. Puissance souscrite : 9 kVA.'],
    ['Article 4 - Résiliation', 'Le client peut résilier à tout moment, sans frais,',
     'par simple demande écrite. La résiliation prend effet sous 30 jours.'],
    ['Article 7 - Réclamations', 'Service client : 09 00 00 00 00 (numéro fictif).',
     'Médiateur national de l\'énergie en cas de litige non résolu sous deux mois.']], 'contrat')

# --- Logement -------------------------------------------------------------------------
doc('Bail_appartement.pdf', 'logement', 'SCI Les Tilleuls', '01/07/2024', None, [
    ['# Contrat de location - logement vide', 'Bailleur : SCI Les Tilleuls', f'Locataire : {ME[0]}',
     'Date : 01/07/2024', 'Logement : appartement T3 de 68 m2, 12 rue des Lilas, 74000 Annecy',
     'Loyer mensuel hors charges : 890,00 €', 'Provision pour charges : 75,00 €',
     'Dépôt de garantie : 890,00 €', 'Durée : 3 ans à compter du 1er juillet 2024.'],
    ['Préavis du locataire : un mois (zone tendue).', 'Le loyer est révisé chaque année selon l\'IRL.']],
    'contrat')
for file, month, date in [('quittance_02-2026.pdf', 'février 2026', '28/02/2026'),
                          ('quittance_03-2026.pdf', 'mars 2026', '31/03/2026')]:
    doc(file, 'logement', 'SCI Les Tilleuls', date, '965,00 €', _invoice(
        f'Quittance de loyer - {month}', 'SCI Les Tilleuls',
        ['Loyer : 890,00 €', 'Charges : 75,00 €'], '965,00 €', date, 'Q' + date[3:5]), 'quittance')
doc('facture_eau_S2_2025.pdf', 'logement', 'Eaux du Val', '15/01/2026', '86,40 €', _invoice(
    'Facture d\'eau - second semestre 2025', 'Eaux du Val',
    ['Consommation : 24 m3', 'Abonnement : 18,20 €'], '86,40 €', '15/01/2026', 'EV-2025-S2'), 'facture')
doc('attestation-habitation.pdf', 'logement', 'Mutuelle Alpes Assurances', '01/07/2025', None, [
    ['# Mutuelle Alpes Assurances', 'Attestation d\'assurance habitation', 'Date : 01/07/2025', *ME,
     'Contrat n° MAH-554210 - multirisque habitation', 'Période de validité : du 01/07/2025 au 30/06/2026',
     'Garanties : incendie, dégâts des eaux, vol, responsabilité civile.']], 'attestation')
doc('devis.pdf', 'logement', 'Isolex', '20/03/2026', '3 480,00 €', _invoice(
    'Devis - isolation des combles perdus', 'Isolex',
    ['Surface : 68 m2 - laine soufflée R=7', 'Main d\'oeuvre et matériaux : 3 480,00 €',
     'Devis valable 3 mois. Éligible aux aides à la rénovation énergétique.'],
    '3 480,00 €', '20/03/2026', 'D-2026-118'), 'devis')

# --- Impôts ---------------------------------------------------------------------------
doc('Avis_2025.pdf', 'impots', 'Direction générale des Finances publiques', '28/07/2025', '2 914,00 €', [
    ['# Avis d\'impôt 2025 sur les revenus de l\'année 2024', 'Date : 28/07/2025', *ME,
     'Revenu fiscal de référence : 38 420 €', 'Nombre de parts : 1',
     'Impôt sur le revenu net : 2 914,00 €', 'Prélèvement à la source déjà versé : 2 760,00 €',
     'Reste à payer : 154,00 €, prélevé en 4 fois de septembre à décembre 2025.']], 'avis')

# --- Banque ---------------------------------------------------------------------------
doc('releve_032026.pdf', 'banque', 'Banque Alpine', '31/03/2026', None, [
    ['# Banque Alpine', 'Relevé de compte courant - mars 2026', 'Date : 31/03/2026', *ME,
     'Solde au 01/03/2026 : 2 318,45 €', '04/03 Prélèvement Voltéa Énergie : -268,92 €',
     '05/03 Virement salaire Technova SAS : +2 640,18 €', '31/03 Loyer SCI Les Tilleuls : -965,00 €',
     'Solde au 31/03/2026 : 3 104,71 €']], 'releve')
doc('offre_pret_auto.pdf', 'banque', 'Banque Alpine', '10/01/2025', '18 000,00 €', [
    ['# Banque Alpine', 'Offre de prêt personnel - financement véhicule', 'Date : 10/01/2025', *ME,
     'Montant emprunté : 18 000,00 €', 'Durée : 48 mois', 'Taux annuel effectif global : 4,20 %',
     'Mensualité : 408,05 €']], 'contrat')

# --- Santé ----------------------------------------------------------------------------
doc('scan_ordonnance.pdf', 'sante', 'Dr Sophie Martin', '14/02/2026', None, [
    ['# Dr Sophie Martin - médecin généraliste', 'Ordonnance', 'Date : 14/02/2026', f'Patient : {ME[0]}',
     'Amoxicilline 1 g : 1 comprimé matin et soir pendant 6 jours.', 'Paracétamol 1 g si douleur.']],
    'ordonnance')
doc('decompte-mutuelle-fev.pdf', 'sante', 'Santé Plus Mutuelle', '03/03/2026', '18,75 €', _invoice(
    'Décompte de remboursement', 'Santé Plus Mutuelle',
    ['Consultation du 14/02/2026 - Dr Martin', 'Part Sécurité sociale : 18,75 €',
     'Part mutuelle : 7,50 €'], '18,75 €', '03/03/2026', 'DM-0226'), 'decompte')

# --- Véhicule -------------------------------------------------------------------------
doc('attestation_auto.pdf', 'vehicule', 'Assur\'Route Direct', '15/01/2026', '612,00 €', [
    ['# Assur\'Route Direct', 'Attestation et avis d\'échéance - assurance automobile', 'Date : 15/01/2026',
     *ME, 'Véhicule électrique - immatriculation GH-482-KL', 'Formule tous risques',
     'Cotisation annuelle 2026 : 612,00 €', 'Assistance panne 0 km incluse (batterie comprise).']], 'avis')
doc('facture_garage.pdf', 'vehicule', 'Garage du Centre', '22/10/2025', '189,90 €', _invoice(
    'Facture - entretien véhicule électrique', 'Garage du Centre',
    ['Contrôle batterie haute tension et freinage', 'Remplacement filtre d\'habitacle',
     'Kilométrage : 31 250 km'], '189,90 €', '22/10/2025', 'GC-10457'), 'facture')

# --- Travail --------------------------------------------------------------------------
for file, month, date in [('BS_2026-01.pdf', 'janvier 2026', '31/01/2026'),
                          ('BS_2026-02.pdf', 'février 2026', '28/02/2026')]:
    doc(file, 'travail', 'Technova SAS', date, '2 640,18 €', [
        ['# Technova SAS', f'Bulletin de paie - {month}', f'Date : {date}', f'Salarié : {ME[0]}',
         'Emploi : technicienne support', 'Salaire de base : 3 350,00 €',
         'Net à payer avant impôt : 2 812,40 €', 'Impôt sur le revenu prélevé à la source : 172,22 €',
         'Net payé : 2 640,18 €']], 'bulletin')

# --- Télécom --------------------------------------------------------------------------
for file, month, date in [('Fibra_facture_0226.pdf', 'février 2026', '02/03/2026'),
                          ('Fibra_facture_0326.pdf', 'mars 2026', '02/04/2026')]:
    doc(file, 'telecom', 'Fibra Télécom', date, '34,99 €', _invoice(
        f'Facture box fibre - {month}', 'Fibra Télécom', ['Abonnement fibre 2 Gb/s : 34,99 €'],
        '34,99 €', date, 'FT-' + date[3:5]), 'facture')
doc('mobile.pdf', 'telecom', 'Mobilo', '05/03/2026', '12,99 €', _invoice(
    'Facture forfait mobile - février 2026', 'Mobilo', ['Forfait 100 Go : 12,99 €'], '12,99 €',
    '05/03/2026', 'MB-226'), 'facture')

# --- Achats ---------------------------------------------------------------------------
doc('commande_88271.pdf', 'achats', 'Informa Plus', '18/11/2025', '1 099,00 €', _invoice(
    'Facture - ordinateur portable 14 pouces', 'Informa Plus',
    ['Ordinateur portable, 16 Go de mémoire, SSD 1 To', 'Garantie constructeur : 2 ans, jusqu\'au 18/11/2027'],
    '1 099,00 €', '18/11/2025', 'IP-88271'), 'facture')
doc('lave-linge.pdf', 'achats', 'Électro Maison', '03/06/2025', '549,00 €', _invoice(
    'Facture - lave-linge 9 kg classe A', 'Électro Maison',
    ['Lave-linge hublot 9 kg, livraison et installation', 'Extension de garantie 5 ans incluse'],
    '549,00 €', '03/06/2025', 'EM-3321'), 'facture')

# --- Scan sans texte (à repérer comme tel) ------------------------------------------------
doc('Scan_0012.pdf', 'autre', None, None, None, [['']], 'scan')


def build(folder: Path) -> list[dict]:
    folder.mkdir(parents=True, exist_ok=True)
    manifest = []
    for item in DOCS:
        write_pdf(folder / item['file'], item['pages'], title='')
        manifest.append({k: v for k, v in item.items() if k != 'pages'})
    (folder / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding='utf-8')
    return manifest


if __name__ == '__main__':
    target = Path(sys.argv[1] if len(sys.argv) > 1 else 'corpus')
    print(f'{len(build(target))} documents écrits dans {target}')
