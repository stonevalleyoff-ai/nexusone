package fr.rbb.tableaudebord.widget

import android.content.Context
import androidx.compose.runtime.Composable
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.glance.GlanceId
import androidx.glance.GlanceModifier
import androidx.glance.GlanceTheme
import androidx.glance.action.actionStartActivity
import androidx.glance.action.clickable
import androidx.glance.appwidget.GlanceAppWidget
import androidx.glance.appwidget.GlanceAppWidgetReceiver
import androidx.glance.appwidget.cornerRadius
import androidx.glance.appwidget.provideContent
import androidx.glance.background
import androidx.glance.layout.Column
import androidx.glance.layout.fillMaxSize
import androidx.glance.layout.padding
import androidx.glance.text.FontWeight
import androidx.glance.text.Text
import androidx.glance.text.TextStyle
import fr.rbb.tableaudebord.TdbApp
import fr.rbb.tableaudebord.ui.MainActivity
import fr.rbb.tableaudebord.ui.jourLisible
import fr.rbb.tableaudebord.ui.joursRestants

/**
 * Widget d'écran d'accueil : nombre de points à traiter et prochaine échéance.
 * Volontairement sans détail (titres des tâches) : il est visible téléphone verrouillé.
 */
class TdbWidget : GlanceAppWidget() {

    override suspend fun provideGlance(context: Context, id: GlanceId) {
        val vue = (context.applicationContext as TdbApp).depot.vueMaintenant()
        val nombre = vue.aTraiter.size
        val prochaine = vue.echeances.firstOrNull { (joursRestants(it.first.jour) ?: -1) >= 0 }
        val ligneEcheance = prochaine?.let { "Prochaine échéance " + jourLisible(it.first.jour) } ?: "Aucune échéance à venir"
        provideContent {
            GlanceTheme {
                Contenu(nombre, ligneEcheance)
            }
        }
    }

    @Composable
    private fun Contenu(nombre: Int, ligneEcheance: String) {
        Column(
            modifier = GlanceModifier
                .fillMaxSize()
                .background(GlanceTheme.colors.widgetBackground)
                .cornerRadius(16.dp)
                .padding(12.dp)
                .clickable(actionStartActivity<MainActivity>()),
        ) {
            Text(
                text = if (nombre == 0) "Rien à traiter" else "$nombre à traiter",
                style = TextStyle(fontSize = 20.sp, fontWeight = FontWeight.Bold, color = GlanceTheme.colors.onSurface),
            )
            Text(
                text = ligneEcheance,
                style = TextStyle(fontSize = 13.sp, color = GlanceTheme.colors.onSurfaceVariant),
            )
        }
    }
}

class TdbWidgetReceiver : GlanceAppWidgetReceiver() {
    override val glanceAppWidget: GlanceAppWidget = TdbWidget()
}
