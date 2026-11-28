package fr.rbb.tableaudebord.ui.screens

import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Add
import androidx.compose.material.icons.outlined.AutoAwesome
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.ExtendedFloatingActionButton
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import fr.rbb.tableaudebord.model.Echeance
import fr.rbb.tableaudebord.net.EvenementAgenda
import fr.rbb.tableaudebord.ui.CarteVide
import fr.rbb.tableaudebord.ui.ajouterEvenement
import fr.rbb.tableaudebord.ui.heureLisible
import fr.rbb.tableaudebord.ui.jourLisible
import fr.rbb.tableaudebord.ui.ouvrirAgenda

private data class LigneAgenda(
    val jour: String,
    val titre: String,
    val heure: String?,
    val lieu: String?,
    val lien: String?,
    val deClaude: Boolean,
)

@Composable
fun EcranAgenda(evenements: List<EvenementAgenda>?, echeancesClaude: List<Echeance>) {
    val contexte = LocalContext.current
    val idsClaude = echeancesClaude.mapNotNull { it.agendaId }.toSet()

    // En ligne : les 30 prochains jours de Google Agenda. Hors ligne : les échéances des synthèses.
    val lignes: List<LigneAgenda> = evenements?.mapNotNull { ev ->
        val jour = ev.start?.date ?: ev.start?.dateHeure?.take(10) ?: return@mapNotNull null
        val titre = ev.summary ?: "(sans titre)"
        LigneAgenda(
            jour = jour,
            titre = titre,
            heure = heureLisible(ev.start?.dateHeure),
            lieu = ev.location,
            lien = ev.htmlLink,
            deClaude = ev.id in idsClaude || titre.startsWith("⏰"),
        )
    } ?: echeancesClaude.mapNotNull { e ->
        val jour = e.jour ?: return@mapNotNull null
        LigneAgenda(jour, e.titre, heureLisible(e.debut), null, null, true)
    }

    Box(Modifier.fillMaxSize()) {
        LazyColumn(
            contentPadding = PaddingValues(start = 16.dp, end = 16.dp, top = 16.dp, bottom = 96.dp),
            verticalArrangement = Arrangement.spacedBy(8.dp),
        ) {
            if (evenements == null) {
                item { CarteVide("Agenda hors ligne : seules les échéances repérées par Claude sont affichées.") }
            }
            if (lignes.isEmpty()) item { CarteVide("Rien de prévu dans les 30 prochains jours.") }
            lignes.groupBy { it.jour }.toSortedMap().forEach { (jour, duJour) ->
                item(key = "j$jour") {
                    Text(
                        jourLisible(jour).replaceFirstChar { it.uppercase() },
                        style = MaterialTheme.typography.titleSmall,
                        modifier = Modifier.padding(top = 8.dp),
                    )
                }
                duJour.forEachIndexed { i, l ->
                    item(key = "e$jour#$i") {
                        Card(
                            Modifier.fillMaxWidth().clickable { ouvrirAgenda(contexte, l.jour, l.lien) },
                            colors = if (l.deClaude) {
                                CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.secondaryContainer)
                            } else {
                                CardDefaults.cardColors()
                            },
                        ) {
                            Row(Modifier.padding(12.dp), verticalAlignment = Alignment.CenterVertically) {
                                Text(l.heure ?: "Journée", style = MaterialTheme.typography.labelLarge, modifier = Modifier.width(72.dp))
                                Column(Modifier.weight(1f)) {
                                    Text(l.titre, fontWeight = FontWeight.Medium)
                                    l.lieu?.let { Text(it, style = MaterialTheme.typography.bodySmall) }
                                }
                                if (l.deClaude) {
                                    Icon(Icons.Outlined.AutoAwesome, contentDescription = "Ajouté par Claude")
                                }
                            }
                        }
                    }
                }
            }
        }
        ExtendedFloatingActionButton(
            onClick = { ajouterEvenement(contexte) },
            icon = { Icon(Icons.Filled.Add, contentDescription = null) },
            text = { Text("Événement") },
            modifier = Modifier.align(Alignment.BottomEnd).padding(16.dp),
        )
    }
}
