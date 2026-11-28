# Tableau de bord

Appli Android personnelle qui affiche ce que Claude tire chaque matin de tes boîtes mail : tâches, échéances, documents et conseils.

- Les données viennent du dossier Google Drive « Tableau de bord », écrit par les tris planifiés de Claude (Gmail à 6h42, laposte.net à 6h40).
- L'appli ne lit jamais tes mails. Elle demande seulement : lecture de Drive, écriture de ses propres fichiers dans Drive (retours.json), lecture de l'agenda.
- Tout reste sur le téléphone, dans une base chiffrée (SQLCipher) dont la clé est dans le coffre Android. Verrou par empreinte ou code. Pas de serveur, pas de statistiques.

## Mise en route (une seule fois, environ 30 minutes)

### 1. Créer ta clé de signature

La clé sert à signer l'appli. Google la reconnaît grâce à son empreinte SHA-1 : garde-la précieusement, sans elle il faudra tout reconfigurer.

Dans PowerShell (keytool est fourni avec Android Studio) :

```powershell
cd $HOME\Documents
& "C:\Program Files\Android\Android Studio\jbr\bin\keytool.exe" -genkeypair -v -keystore tdb.jks -alias tdb -keyalg RSA -keysize 2048 -validity 10000
```

Choisis un mot de passe et note-le. Puis affiche l'empreinte SHA-1 :

```powershell
& "C:\Program Files\Android\Android Studio\jbr\bin\keytool.exe" -list -v -keystore tdb.jks -alias tdb
```

Copie la ligne `SHA1: AA:BB:...`.

### 2. Projet Google Cloud

1. Va sur https://console.cloud.google.com, connecté avec shinjiebi@gmail.com, et crée un projet « Tableau de bord ».
2. **API et services → Bibliothèque** : active **Google Drive API** et **Google Calendar API**.
3. **Écran de consentement OAuth** : type **Externe**, nom « Tableau de bord », ton adresse en contact. Dans **Utilisateurs test**, ajoute `shinjiebi@gmail.com`. Laisse l'appli en mode **Test**.
4. **Identifiants → Créer des identifiants → ID client OAuth** : type **Android**, nom de package `fr.rbb.tableaudebord`, empreinte SHA-1 de l'étape 1.

En mode Test, Google peut redemander ton accord environ une fois par semaine : l'appli affiche alors l'écran de connexion Google, il suffit de valider.

### 3. Dépôt GitHub privé

1. Crée un dépôt **privé** `tableau-de-bord` sur GitHub.
2. Envoie-y ce dossier (avec GitHub Desktop, ou en ligne de commande) :

```powershell
cd <dossier TableauDeBord>
git init
git add .
git commit -m "Première version"
git branch -M main
git remote add origin https://github.com/<ton-compte>/tableau-de-bord.git
git push -u origin main
```

Le fichier `tdb.jks` n'est jamais envoyé (il est dans `.gitignore`).

3. Dans le dépôt : **Settings → Secrets and variables → Actions → New repository secret**, crée 4 secrets :

| Secret | Valeur |
| --- | --- |
| `KEYSTORE_BASE64` | le fichier tdb.jks encodé (commande ci-dessous) |
| `KEYSTORE_PASSWORD` | le mot de passe de l'étape 1 |
| `KEY_ALIAS` | `tdb` |
| `KEY_PASSWORD` | le même mot de passe |

```powershell
[Convert]::ToBase64String([IO.File]::ReadAllBytes("$HOME\Documents\tdb.jks")) | Set-Clipboard
```

(la valeur est copiée dans le presse-papiers, colle-la dans le secret).

### 4. Compiler et installer

- Onglet **Actions** du dépôt → **Build APK** → **Run workflow** (ou tout simplement un nouveau `git push`).
- Quand c'est vert, ouvre le run, télécharge l'artefact `TableauDeBord-N`, décompresse-le et envoie l'APK sur ton téléphone.
- Ouvre l'APK sur le téléphone et autorise l'installation depuis cette source.

Au premier lancement : empreinte, puis écran Google pour autoriser Drive et l'agenda.

## Compiler sur ton PC (facultatif)

Ouvre le dossier dans Android Studio. Pour signer avec ta clé, crée un fichier `keystore.properties` à la racine (jamais commité) :

```
storeFile=C:/Users/<toi>/Documents/tdb.jks
storePassword=...
keyAlias=tdb
keyPassword=...
```

## Échanges avec Claude

| Fichier dans Drive « Tableau de bord » | Écrit par | Lu par |
| --- | --- | --- |
| `synthese-gmail-AAAA-MM-JJ.json` | tri Gmail de Claude | l'appli (le plus récent) |
| `synthese-laposte-AAAA-MM-JJ.json` | tri laposte.net de Claude | l'appli (le plus récent) |
| `retours.json` | l'appli | Claude, au tri suivant |

Dans `retours.json`, chaque tâche a un statut : `fait`, `ignore`, `reporte` (avec `nouvelle_echeance`) ou `note`. Les questions posées dans l'onglet Conseils y sont aussi ; Claude y répond dans les conseils du lendemain.

## Limites de cette version

- La synchronisation se fait à l'ouverture et avec le bouton Actualiser : pas de notification en arrière-plan.
- Le tri laposte.net dépend de ton PC allumé avec l'appli Claude ouverte. Sinon, l'accueil signale une synthèse « pas à jour ».
- Les documents s'ouvrent en lecture : pas de modification ni de suppression dans Drive depuis l'appli.
