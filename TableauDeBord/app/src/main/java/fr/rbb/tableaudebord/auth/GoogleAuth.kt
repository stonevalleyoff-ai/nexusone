package fr.rbb.tableaudebord.auth

import android.app.Activity
import android.app.PendingIntent
import android.content.Intent
import com.google.android.gms.auth.api.identity.AuthorizationRequest
import com.google.android.gms.auth.api.identity.Identity
import com.google.android.gms.common.api.Scope
import fr.rbb.tableaudebord.Config
import kotlinx.coroutines.tasks.await

/**
 * Autorisation Google via les services Play : pas de mot de passe stocké dans l'appli,
 * seulement un jeton d'accès d'une heure, redemandé à chaque ouverture.
 */
class GoogleAuth {

    sealed interface Resultat {
        data class Ok(val jeton: String) : Resultat
        data class ConsentementRequis(val pendingIntent: PendingIntent) : Resultat
        data class Erreur(val message: String) : Resultat
    }

    @Volatile
    var jeton: String? = null
        private set

    fun oublierJeton() {
        jeton = null
    }

    private fun requete(): AuthorizationRequest =
        AuthorizationRequest.builder()
            .setRequestedScopes(Config.SCOPES.map { Scope(it) })
            .build()

    suspend fun autoriser(activity: Activity): Resultat = try {
        val r = Identity.getAuthorizationClient(activity).authorize(requete()).await()
        val pi = r.pendingIntent
        val t = r.accessToken
        when {
            r.hasResolution() && pi != null -> Resultat.ConsentementRequis(pi)
            t != null -> {
                jeton = t
                Resultat.Ok(t)
            }
            else -> Resultat.Erreur("Google n'a pas renvoyé de jeton d'accès.")
        }
    } catch (e: Exception) {
        Resultat.Erreur(e.message ?: e.javaClass.simpleName)
    }

    /** Appelé au retour de l'écran de consentement Google. */
    fun depuisIntent(activity: Activity, data: Intent?): Resultat = try {
        // Écran fermé sans réponse : pas d'Intent à lire.
        if (data == null) throw IllegalStateException("Autorisation refusée.")
        val r = Identity.getAuthorizationClient(activity).getAuthorizationResultFromIntent(data)
        val t = r.accessToken
        if (t != null) {
            jeton = t
            Resultat.Ok(t)
        } else {
            Resultat.Erreur("Autorisation refusée.")
        }
    } catch (e: Exception) {
        Resultat.Erreur(e.message ?: "Autorisation refusée.")
    }
}
