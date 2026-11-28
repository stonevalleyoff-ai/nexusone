package fr.rbb.tableaudebord.ui

import android.content.ActivityNotFoundException
import android.content.ContentUris
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.provider.CalendarContract
import android.webkit.MimeTypeMap
import android.widget.Toast
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.Card
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.core.content.FileProvider
import java.io.File
import java.time.LocalDate
import java.time.OffsetDateTime
import java.time.ZoneId
import java.time.format.DateTimeFormatter
import java.time.temporal.ChronoUnit
import java.util.Locale

private val FR = Locale.FRENCH
private val formatJour = DateTimeFormatter.ofPattern("EEE d MMM", FR)
private val formatJourAnnee = DateTimeFormatter.ofPattern("EEE d MMM yyyy", FR)
private val formatHeure = DateTimeFormatter.ofPattern("HH'h'mm", FR)

fun jourLisible(iso: String?): String {
    if (iso.isNullOrBlank()) return ""
    val d = runCatching { LocalDate.parse(iso.take(10)) }.getOrNull() ?: return iso
    val aujourdHui = LocalDate.now()
    return when (ChronoUnit.DAYS.between(aujourdHui, d)) {
        0L -> "aujourd'hui"
        1L -> "demain"
        -1L -> "hier"
        else -> if (d.year == aujourdHui.year) formatJour.format(d) else formatJourAnnee.format(d)
    }
}

fun heureLisible(isoDateHeure: String?): String? =
    isoDateHeure?.let { runCatching { formatHeure.format(OffsetDateTime.parse(it)) }.getOrNull() }

fun joursRestants(iso: String?): Long? =
    iso?.let { runCatching { ChronoUnit.DAYS.between(LocalDate.now(), LocalDate.parse(it.take(10))) }.getOrNull() }

fun dateHeureLisible(dt: OffsetDateTime?): String {
    if (dt == null) return "jamais"
    val local = dt.atZoneSameInstant(ZoneId.systemDefault())
    return jourLisible(local.toLocalDate().toString()) + " à " + formatHeure.format(local)
}

@Composable
fun Section(titre: String, modifier: Modifier = Modifier, contenu: @Composable () -> Unit) {
    Column(modifier.fillMaxWidth().padding(horizontal = 16.dp, vertical = 8.dp)) {
        Text(titre, style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.SemiBold)
        Spacer(Modifier.height(8.dp))
        contenu()
    }
}

@Composable
fun CarteVide(texte: String) {
    Card(Modifier.fillMaxWidth()) {
        Text(texte, Modifier.padding(16.dp), style = MaterialTheme.typography.bodyMedium)
    }
}

// ---------- Ouvertures vers d'autres applis ----------

private fun lancer(context: Context, intent: Intent, siEchec: String) {
    try {
        context.startActivity(intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
    } catch (e: ActivityNotFoundException) {
        Toast.makeText(context, siEchec, Toast.LENGTH_LONG).show()
    }
}

/** Ouvre Google Agenda au jour donné (ou l'événement via son lien s'il est connu). */
fun ouvrirAgenda(context: Context, jourIso: String?, lien: String? = null) {
    if (lien != null) {
        lancer(context, Intent(Intent.ACTION_VIEW, Uri.parse(lien)), "Impossible d'ouvrir l'agenda")
        return
    }
    val jour = jourIso?.let { runCatching { LocalDate.parse(it.take(10)) }.getOrNull() } ?: LocalDate.now()
    val ms = jour.atTime(8, 0).atZone(ZoneId.systemDefault()).toInstant().toEpochMilli()
    val uri = CalendarContract.CONTENT_URI.buildUpon().appendPath("time").let { ContentUris.appendId(it, ms) }.build()
    lancer(context, Intent(Intent.ACTION_VIEW, uri), "Aucune appli d'agenda trouvée")
}

/** Ouvre l'écran « nouvel événement » de l'agenda : aucune autorisation d'écriture nécessaire. */
fun ajouterEvenement(context: Context, titre: String = "", jourIso: String? = null) {
    val intent = Intent(Intent.ACTION_INSERT, CalendarContract.Events.CONTENT_URI)
        .putExtra(CalendarContract.Events.TITLE, titre)
    jourIso?.let { runCatching { LocalDate.parse(it.take(10)) }.getOrNull() }?.let { j ->
        val ms = j.atTime(9, 0).atZone(ZoneId.systemDefault()).toInstant().toEpochMilli()
        intent.putExtra(CalendarContract.EXTRA_EVENT_BEGIN_TIME, ms)
        intent.putExtra(CalendarContract.EXTRA_EVENT_END_TIME, ms + 30 * 60 * 1000)
    }
    lancer(context, intent, "Aucune appli d'agenda trouvée")
}

private fun uriFichier(context: Context, f: File): Uri =
    FileProvider.getUriForFile(context, context.packageName + ".files", f)

private fun typeMime(f: File): String =
    MimeTypeMap.getSingleton().getMimeTypeFromExtension(f.extension.lowercase()) ?: "application/octet-stream"

fun ouvrirAvecAutreAppli(context: Context, f: File) {
    val intent = Intent(Intent.ACTION_VIEW)
        .setDataAndType(uriFichier(context, f), typeMime(f))
        .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
    lancer(context, intent, "Aucune appli ne sait ouvrir ce fichier")
}

fun partager(context: Context, f: File, titre: String) {
    val envoi = Intent(Intent.ACTION_SEND)
        .setType(typeMime(f))
        .putExtra(Intent.EXTRA_STREAM, uriFichier(context, f))
        .putExtra(Intent.EXTRA_SUBJECT, titre)
        .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
    lancer(context, Intent.createChooser(envoi, "Partager"), "Partage impossible")
}
