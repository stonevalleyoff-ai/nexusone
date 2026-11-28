package fr.rbb.tableaudebord.ui

import androidx.biometric.BiometricManager
import androidx.biometric.BiometricManager.Authenticators.BIOMETRIC_WEAK
import androidx.biometric.BiometricManager.Authenticators.DEVICE_CREDENTIAL
import androidx.biometric.BiometricPrompt
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.outlined.Lock
import androidx.compose.material3.Button
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import androidx.core.content.ContextCompat
import androidx.fragment.app.FragmentActivity

private const val AUTORISES = BIOMETRIC_WEAK or DEVICE_CREDENTIAL

/** Vrai si le téléphone a une empreinte, un visage ou un code de verrouillage configuré. */
fun verrouDisponible(activity: FragmentActivity): Boolean =
    BiometricManager.from(activity).canAuthenticate(AUTORISES) == BiometricManager.BIOMETRIC_SUCCESS

fun demanderDeverrouillage(activity: FragmentActivity, reussi: () -> Unit) {
    if (!verrouDisponible(activity)) {
        // Aucun verrou sur le téléphone : on ne peut rien demander de plus.
        reussi()
        return
    }
    val prompt = BiometricPrompt(
        activity,
        ContextCompat.getMainExecutor(activity),
        object : BiometricPrompt.AuthenticationCallback() {
            override fun onAuthenticationSucceeded(result: BiometricPrompt.AuthenticationResult) {
                reussi()
            }
        },
    )
    val infos = BiometricPrompt.PromptInfo.Builder()
        .setTitle("Tableau de bord")
        .setSubtitle("Déverrouille pour voir tes tâches et documents")
        .setAllowedAuthenticators(AUTORISES)
        .build()
    prompt.authenticate(infos)
}

@Composable
fun EcranVerrou(activity: FragmentActivity, deverrouiller: () -> Unit) {
    LaunchedEffect(Unit) { demanderDeverrouillage(activity, deverrouiller) }
    Column(
        Modifier.fillMaxSize().padding(32.dp),
        verticalArrangement = Arrangement.Center,
        horizontalAlignment = Alignment.CenterHorizontally,
    ) {
        Icon(Icons.Outlined.Lock, contentDescription = null, modifier = Modifier.size(64.dp))
        Text("Tableau de bord verrouillé", style = MaterialTheme.typography.titleLarge, modifier = Modifier.padding(16.dp))
        Button(onClick = { demanderDeverrouillage(activity, deverrouiller) }) { Text("Déverrouiller") }
    }
}
