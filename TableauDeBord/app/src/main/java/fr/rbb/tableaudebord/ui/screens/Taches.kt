package fr.rbb.tableaudebord.ui.screens

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.MoreVert
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.Checkbox
import androidx.compose.material3.DatePicker
import androidx.compose.material3.DatePickerDialog
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.rememberDatePickerState
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextDecoration
import androidx.compose.ui.unit.dp
import fr.rbb.tableaudebord.data.TacheVue
import fr.rbb.tableaudebord.ui.AppViewModel
import fr.rbb.tableaudebord.ui.CarteVide
import fr.rbb.tableaudebord.ui.jourLisible
import fr.rbb.tableaudebord.ui.joursRestants
import fr.rbb.tableaudebord.ui.theme.RougeUrgent
import java.time.Instant
import java.time.ZoneOffset

@Composable
fun EcranTaches(
    taches: List<TacheVue>,
    marquer: (id: String, statut: String, nouvelleEcheance: String?, note: String?) -> Unit,
    annuler: (id: String) -> Unit,
) {
    var aReporter by remember { mutableStateOf<TacheVue?>(null) }
    var aNoter by remember { mutableStateOf<TacheVue?>(null) }

    val ouvertes = taches.filterNot { it.terminee }
    val fermees = taches.filter { it.terminee }

    LazyColumn(
        contentPadding = PaddingValues(16.dp),
        verticalArrangement = Arrangement.spacedBy(8.dp),
    ) {
        if (ouvertes.isEmpty()) item { CarteVide("Aucune tâche ouverte.") }
        items(ouvertes) { t ->
            CarteTache(
                t = t,
                cocher = { marquer(t.tache.id, "fait", null, null) },
                reporter = { aReporter = t },
                ignorer = { marquer(t.tache.id, "ignore", null, null) },
                noter = { aNoter = t },
                annuler = { annuler(t.tache.id) },
            )
        }
        if (fermees.isNotEmpty()) {
            item {
                Text(
                    "Faites ou ignorées : Claude les retire au prochain tri",
                    style = MaterialTheme.typography.titleSmall,
                    modifier = Modifier.padding(top = 16.dp),
                )
            }
            items(fermees) { t ->
                CarteTache(t, cocher = { annuler(t.tache.id) }, reporter = {}, ignorer = {}, noter = {}, annuler = { annuler(t.tache.id) })
            }
        }
    }

    aReporter?.let { t ->
        DialogueReport(
            fermer = { aReporter = null },
            valider = { jour ->
                marquer(t.tache.id, "reporte", jour, null)
                aReporter = null
            },
        )
    }
    aNoter?.let { t ->
        DialogueNote(
            initiale = t.etat?.note ?: "",
            fermer = { aNoter = null },
            valider = { note ->
                marquer(t.tache.id, t.etat?.statut ?: "note", t.etat?.nouvelleEcheance, note)
                aNoter = null
            },
        )
    }
}

@Composable
private fun CarteTache(
    t: TacheVue,
    cocher: () -> Unit,
    reporter: () -> Unit,
    ignorer: () -> Unit,
    noter: () -> Unit,
    annuler: () -> Unit,
) {
    var menu by remember { mutableStateOf(false) }
    val restants = joursRestants(t.echeanceEffective)
    val urgent = !t.terminee && (t.tache.priorite == "haute" || (restants != null && restants <= 3))
    Card(
        Modifier.fillMaxWidth(),
        colors = if (urgent) CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.errorContainer) else CardDefaults.cardColors(),
    ) {
        Row(Modifier.padding(start = 4.dp, end = 4.dp, top = 8.dp, bottom = 8.dp), verticalAlignment = Alignment.Top) {
            Checkbox(checked = t.terminee, onCheckedChange = { cocher() })
            Column(Modifier.weight(1f).padding(top = 10.dp)) {
                Text(
                    t.tache.titre,
                    fontWeight = FontWeight.Medium,
                    textDecoration = if (t.terminee) TextDecoration.LineThrough else null,
                )
                t.tache.action?.let { Text(it, style = MaterialTheme.typography.bodyMedium) }
                t.echeanceEffective?.let { e ->
                    val reporte = t.etat?.statut == "reporte"
                    Text(
                        (if (reporte) "Reportée au " else "Avant le ") + jourLisible(e),
                        style = MaterialTheme.typography.labelMedium,
                        color = if (restants != null && restants < 0) RougeUrgent else MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                }
                t.tache.source?.let { s ->
                    Text(
                        listOfNotNull(AppViewModel.nomBoite(t.boite), s.expediteur, s.objet, jourLisible(s.date).ifBlank { null })
                            .joinToString(" · "),
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                }
                t.etat?.note?.let { Text("Ta note : $it", style = MaterialTheme.typography.bodySmall) }
            }
            Box {
                IconButton(onClick = { menu = true }) { Icon(Icons.Filled.MoreVert, contentDescription = "Actions") }
                DropdownMenu(expanded = menu, onDismissRequest = { menu = false }) {
                    if (t.terminee) {
                        DropdownMenuItem(text = { Text("Remettre à faire") }, onClick = { menu = false; annuler() })
                    } else {
                        DropdownMenuItem(text = { Text("Reporter…") }, onClick = { menu = false; reporter() })
                        DropdownMenuItem(text = { Text("Note pour Claude…") }, onClick = { menu = false; noter() })
                        DropdownMenuItem(text = { Text("Ignorer") }, onClick = { menu = false; ignorer() })
                        if (t.etat != null) {
                            DropdownMenuItem(text = { Text("Annuler mes changements") }, onClick = { menu = false; annuler() })
                        }
                    }
                }
            }
        }
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun DialogueReport(fermer: () -> Unit, valider: (String) -> Unit) {
    val etat = rememberDatePickerState()
    DatePickerDialog(
        onDismissRequest = fermer,
        confirmButton = {
            TextButton(
                enabled = etat.selectedDateMillis != null,
                onClick = {
                    etat.selectedDateMillis?.let { ms ->
                        valider(Instant.ofEpochMilli(ms).atZone(ZoneOffset.UTC).toLocalDate().toString())
                    }
                },
            ) { Text("Reporter") }
        },
        dismissButton = { TextButton(onClick = fermer) { Text("Annuler") } },
    ) {
        DatePicker(state = etat)
    }
}

@Composable
private fun DialogueNote(initiale: String, fermer: () -> Unit, valider: (String) -> Unit) {
    var texte by remember { mutableStateOf(initiale) }
    AlertDialog(
        onDismissRequest = fermer,
        title = { Text("Note pour Claude") },
        text = {
            OutlinedTextField(
                value = texte,
                onValueChange = { texte = it },
                placeholder = { Text("Ex. : déjà payé par virement le 25/11") },
                minLines = 3,
            )
        },
        confirmButton = { TextButton(onClick = { valider(texte.trim()) }, enabled = texte.isNotBlank()) { Text("Enregistrer") } },
        dismissButton = { TextButton(onClick = fermer) { Text("Annuler") } },
    )
}
