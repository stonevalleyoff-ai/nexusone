package fr.rbb.tableaudebord.ui.screens

import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.filled.Search
import androidx.compose.material.icons.outlined.Description
import androidx.compose.material.icons.outlined.Folder
import androidx.compose.material.icons.outlined.PictureAsPdf
import androidx.compose.material.icons.outlined.Share
import androidx.compose.material3.Card
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.unit.dp
import fr.rbb.tableaudebord.net.DriveFichier
import fr.rbb.tableaudebord.ui.CarteVide
import fr.rbb.tableaudebord.ui.EtatDocuments
import fr.rbb.tableaudebord.ui.jourLisible
import fr.rbb.tableaudebord.ui.ouvrirAvecAutreAppli
import fr.rbb.tableaudebord.ui.partager
import java.io.File

@Composable
fun EcranDocuments(
    etat: EtatDocuments,
    ouvrirDossier: (DriveFichier) -> Unit,
    remonter: () -> Unit,
    chercher: (String) -> Unit,
    telecharger: (DriveFichier, (File) -> Unit) -> Unit,
    ouvrirPdf: (File, String) -> Unit,
) {
    val contexte = LocalContext.current
    var saisie by remember(etat.recherche) { mutableStateOf(etat.recherche) }

    Column(Modifier.fillMaxWidth()) {
        OutlinedTextField(
            value = saisie,
            onValueChange = { saisie = it },
            modifier = Modifier.fillMaxWidth().padding(16.dp),
            singleLine = true,
            placeholder = { Text("Chercher un document (ex. GMF, facture)") },
            leadingIcon = { Icon(Icons.Filled.Search, contentDescription = null) },
            keyboardOptions = KeyboardOptions(imeAction = ImeAction.Search),
            keyboardActions = KeyboardActions(onSearch = { chercher(saisie) }),
        )

        Row(Modifier.padding(horizontal = 8.dp), verticalAlignment = Alignment.CenterVertically) {
            if (etat.pile.size > 1 || etat.recherche.isNotEmpty()) {
                IconButton(onClick = { saisie = ""; remonter() }) {
                    Icon(Icons.AutoMirrored.Filled.ArrowBack, contentDescription = "Retour")
                }
            }
            Text(
                if (etat.recherche.isNotEmpty()) "Résultats pour « ${etat.recherche} »"
                else etat.pile.joinToString(" › ") { it.second },
                style = MaterialTheme.typography.titleSmall,
                modifier = Modifier.padding(horizontal = 8.dp),
            )
        }

        if (etat.chargement || etat.telechargement != null) {
            LinearProgressIndicator(Modifier.fillMaxWidth().padding(horizontal = 16.dp))
            etat.telechargement?.let {
                Text("Téléchargement de $it…", style = MaterialTheme.typography.bodySmall, modifier = Modifier.padding(16.dp))
            }
        }
        etat.erreur?.let {
            Text(it, color = MaterialTheme.colorScheme.error, modifier = Modifier.padding(16.dp))
        }

        LazyColumn(
            contentPadding = PaddingValues(16.dp),
            verticalArrangement = Arrangement.spacedBy(8.dp),
        ) {
            if (!etat.chargement && etat.contenu.isEmpty() && etat.erreur == null) {
                item { CarteVide("Aucun document ici. Touche Actualiser si tu viens d'ouvrir l'appli.") }
            }
            items(etat.contenu, key = { it.id }) { f ->
                LigneFichier(
                    f = f,
                    ouvrir = {
                        when {
                            f.estDossier -> ouvrirDossier(f)
                            f.estPdf -> telecharger(f) { fichier -> ouvrirPdf(fichier, f.name) }
                            else -> telecharger(f) { fichier -> ouvrirAvecAutreAppli(contexte, fichier) }
                        }
                    },
                    partager = { telecharger(f) { fichier -> partager(contexte, fichier, f.name) } },
                )
            }
        }
    }
}

@Composable
private fun LigneFichier(f: DriveFichier, ouvrir: () -> Unit, partager: () -> Unit) {
    Card(Modifier.fillMaxWidth().clickable(onClick = ouvrir)) {
        Row(Modifier.padding(12.dp), verticalAlignment = Alignment.CenterVertically) {
            Icon(
                when {
                    f.estDossier -> Icons.Outlined.Folder
                    f.estPdf -> Icons.Outlined.PictureAsPdf
                    else -> Icons.Outlined.Description
                },
                contentDescription = null,
            )
            Spacer(Modifier.width(12.dp))
            Column(Modifier.weight(1f)) {
                Text(f.name, fontWeight = FontWeight.Medium)
                f.modifiedTime?.let {
                    Text("Modifié " + jourLisible(it), style = MaterialTheme.typography.bodySmall)
                }
            }
            if (!f.estDossier) {
                IconButton(onClick = partager) { Icon(Icons.Outlined.Share, contentDescription = "Partager") }
            }
        }
    }
}
