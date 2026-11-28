package fr.rbb.tableaudebord.ui.screens

import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.outlined.Description
import androidx.compose.material.icons.outlined.Event
import androidx.compose.material.icons.outlined.Warning
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import fr.rbb.tableaudebord.data.TacheVue
import fr.rbb.tableaudebord.data.VueTableau
import fr.rbb.tableaudebord.ui.AppViewModel
import fr.rbb.tableaudebord.ui.CarteVide
import fr.rbb.tableaudebord.ui.Section
import fr.rbb.tableaudebord.ui.dateHeureLisible
import fr.rbb.tableaudebord.ui.heureLisible
import fr.rbb.tableaudebord.ui.jourLisible
import fr.rbb.tableaudebord.ui.joursRestants
import fr.rbb.tableaudebord.ui.ouvrirAgenda
import fr.rbb.tableaudebord.ui.theme.RougeUrgent

@Composable
fun EcranAccueil(
    vue: VueTableau,
    versTaches: () -> Unit,
    versDocuments: () -> Unit,
    ouvrirDocument: (driveId: String, titre: String) -> Unit,
) {
    val contexte = LocalContext.current
    val aTraiter = vue.aTraiter
    val semaine = vue.echeances.filter { (joursRestants(it.first.jour) ?: 99) in 0..7 }

    LazyColumn(Modifier.fillMaxWidth()) {
        item {
            Section("Dernier passage de Claude") {
                if (vue.passages.isEmpty()) {
                    CarteVide("Aucune synthèse reçue. Touche Actualiser pour te connecter à Google.")
                }
                vue.passages.forEach { p ->
                    Row(verticalAlignment = Alignment.CenterVertically, modifier = Modifier.padding(vertical = 2.dp)) {
                        if (p.perime) {
                            Icon(Icons.Outlined.Warning, contentDescription = "Pas à jour", tint = RougeUrgent)
                            Spacer(Modifier.width(6.dp))
                        }
                        Text(
                            AppViewModel.nomBoite(p.boite) + " : " + dateHeureLisible(p.genereLe) +
                                if (p.perime) " (pas à jour)" else "",
                            style = MaterialTheme.typography.bodyMedium,
                        )
                    }
                }
            }
        }

        item {
            Section("À traiter (${aTraiter.size})") {
                if (aTraiter.isEmpty()) CarteVide("Rien à traiter.")
                Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                    aTraiter.take(5).forEach { CarteTacheResume(it, versTaches) }
                    if (aTraiter.size > 5) {
                        Text(
                            "Voir les ${aTraiter.size} tâches",
                            color = MaterialTheme.colorScheme.primary,
                            modifier = Modifier.clickable(onClick = versTaches).padding(4.dp),
                        )
                    }
                }
            }
        }

        item {
            Section("Les 7 prochains jours") {
                if (semaine.isEmpty()) CarteVide("Aucune échéance cette semaine.")
                Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                    semaine.forEach { (e, _) ->
                        Card(
                            Modifier.fillMaxWidth().clickable { ouvrirAgenda(contexte, e.jour) },
                        ) {
                            Row(Modifier.padding(12.dp), verticalAlignment = Alignment.CenterVertically) {
                                Icon(Icons.Outlined.Event, contentDescription = null)
                                Spacer(Modifier.width(12.dp))
                                Column {
                                    Text(e.titre, fontWeight = FontWeight.Medium)
                                    val heure = heureLisible(e.debut)
                                    Text(
                                        jourLisible(e.jour) + (heure?.let { " à $it" } ?: ""),
                                        style = MaterialTheme.typography.bodySmall,
                                    )
                                }
                            }
                        }
                    }
                }
            }
        }

        item {
            Section("Derniers documents") {
                if (vue.documents.isEmpty()) CarteVide("Aucun document récent.")
                Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                    vue.documents.take(4).forEach { (d, _) ->
                        Card(
                            Modifier.fillMaxWidth().clickable {
                                val id = d.driveId
                                if (id != null) ouvrirDocument(id, d.titre) else versDocuments()
                            },
                        ) {
                            Row(Modifier.padding(12.dp), verticalAlignment = Alignment.CenterVertically) {
                                Icon(Icons.Outlined.Description, contentDescription = null)
                                Spacer(Modifier.width(12.dp))
                                Column {
                                    Text(d.titre, fontWeight = FontWeight.Medium)
                                    Text(
                                        listOfNotNull(d.type, jourLisible(d.date).ifBlank { null }).joinToString(" · "),
                                        style = MaterialTheme.typography.bodySmall,
                                    )
                                }
                            }
                        }
                    }
                }
            }
        }
    }
}

@Composable
private fun CarteTacheResume(t: TacheVue, onClick: () -> Unit) {
    val restants = joursRestants(t.echeanceEffective)
    val urgent = t.tache.priorite == "haute" || (restants != null && restants <= 3)
    Card(
        Modifier.fillMaxWidth().clickable(onClick = onClick),
        colors = if (urgent) {
            CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.errorContainer)
        } else {
            CardDefaults.cardColors()
        },
    ) {
        Column(Modifier.padding(12.dp)) {
            Text(t.tache.titre, fontWeight = FontWeight.Medium)
            t.tache.action?.let { Text(it, style = MaterialTheme.typography.bodySmall) }
            t.echeanceEffective?.let {
                Text("Avant le " + jourLisible(it), style = MaterialTheme.typography.labelMedium)
            }
        }
    }
}
