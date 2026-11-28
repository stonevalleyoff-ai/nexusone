package fr.rbb.tableaudebord.ui.screens

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.FilterChip
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import fr.rbb.tableaudebord.data.QuestionEntite
import fr.rbb.tableaudebord.model.Conseil
import fr.rbb.tableaudebord.ui.AppViewModel
import fr.rbb.tableaudebord.ui.CarteVide
import java.time.Instant
import java.time.ZoneId

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun EcranConseils(
    conseils: List<Pair<Conseil, String>>,
    questions: List<QuestionEntite>,
    retoursEnAttente: Boolean,
    poser: (boite: String?, texte: String) -> Unit,
) {
    var texte by remember { mutableStateOf("") }
    var boite by remember { mutableStateOf<String?>(null) }

    LazyColumn(
        contentPadding = PaddingValues(16.dp),
        verticalArrangement = Arrangement.spacedBy(8.dp),
    ) {
        item {
            Text("Conseils de Claude", style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.SemiBold)
        }
        if (conseils.isEmpty()) item { CarteVide("Pas encore de conseil. Ils arrivent avec la synthèse du matin.") }
        items(conseils) { (c, b) ->
            Card(Modifier.fillMaxWidth()) {
                Column(Modifier.padding(16.dp)) {
                    Text(c.titre, fontWeight = FontWeight.SemiBold)
                    c.texte?.let { Text(it, style = MaterialTheme.typography.bodyMedium, modifier = Modifier.padding(top = 4.dp)) }
                    Text(
                        AppViewModel.nomBoite(b),
                        style = MaterialTheme.typography.labelSmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                        modifier = Modifier.padding(top = 6.dp),
                    )
                }
            }
        }

        item {
            Text(
                "Demander quelque chose à Claude",
                style = MaterialTheme.typography.titleMedium,
                fontWeight = FontWeight.SemiBold,
                modifier = Modifier.padding(top = 24.dp),
            )
            Text(
                "Ta question part dans retours.json. Claude y répond dans les conseils au prochain tri du matin.",
                style = MaterialTheme.typography.bodySmall,
            )
        }
        item {
            OutlinedTextField(
                value = texte,
                onValueChange = { texte = it },
                modifier = Modifier.fillMaxWidth(),
                placeholder = { Text("Ex. : comment contester les frais de rejet Fortuneo ?") },
                minLines = 3,
            )
        }
        item {
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                FilterChip(selected = boite == null, onClick = { boite = null }, label = { Text("Les deux") })
                FilterChip(selected = boite == "gmail", onClick = { boite = "gmail" }, label = { Text("Gmail") })
                FilterChip(selected = boite == "laposte", onClick = { boite = "laposte" }, label = { Text("La Poste") })
            }
        }
        item {
            Button(
                onClick = {
                    poser(boite, texte)
                    texte = ""
                },
                enabled = texte.isNotBlank(),
            ) { Text("Envoyer à Claude") }
        }
        if (questions.isNotEmpty()) {
            item {
                Text(
                    if (retoursEnAttente) "Tes questions (envoi à la prochaine connexion)" else "Tes questions envoyées",
                    style = MaterialTheme.typography.titleSmall,
                    modifier = Modifier.padding(top = 16.dp),
                )
            }
            items(questions, key = { it.id }) { q ->
                Card(Modifier.fillMaxWidth()) {
                    Column(Modifier.padding(12.dp)) {
                        Text(q.texte)
                        val jour = Instant.ofEpochMilli(q.le).atZone(ZoneId.systemDefault()).toLocalDate()
                        Text(
                            "${jour.dayOfMonth}/${jour.monthValue} · " + (q.boite?.let { AppViewModel.nomBoite(it) } ?: "les deux boîtes"),
                            style = MaterialTheme.typography.labelSmall,
                        )
                    }
                }
            }
        }
    }
}
