package fr.rbb.tableaudebord.ui.theme

import android.os.Build
import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.dynamicDarkColorScheme
import androidx.compose.material3.dynamicLightColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext

private val Bleu = Color(0xFF1F4E79)

private val Clair = lightColorScheme(primary = Bleu)
private val Sombre = darkColorScheme(primary = Color(0xFF9CC7F0))

/** Couleur d'alerte pour les tâches urgentes, lisible en clair et en sombre. */
val RougeUrgent = Color(0xFFC62828)

@Composable
fun TdbTheme(content: @Composable () -> Unit) {
    val sombre = isSystemInDarkTheme()
    val contexte = LocalContext.current
    val couleurs = when {
        Build.VERSION.SDK_INT >= Build.VERSION_CODES.S ->
            if (sombre) dynamicDarkColorScheme(contexte) else dynamicLightColorScheme(contexte)
        sombre -> Sombre
        else -> Clair
    }
    MaterialTheme(colorScheme = couleurs, content = content)
}
