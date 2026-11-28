package fr.rbb.tableaudebord.ui

import android.app.Activity
import android.app.Application
import android.app.PendingIntent
import android.content.Intent
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import fr.rbb.tableaudebord.Config
import fr.rbb.tableaudebord.TdbApp
import fr.rbb.tableaudebord.auth.GoogleAuth
import fr.rbb.tableaudebord.data.QuestionEntite
import fr.rbb.tableaudebord.data.VueTableau
import fr.rbb.tableaudebord.net.DriveFichier
import fr.rbb.tableaudebord.net.ErreurApi
import fr.rbb.tableaudebord.net.EvenementAgenda
import fr.rbb.tableaudebord.net.JetonInvalide
import fr.rbb.tableaudebord.widget.TdbWidget
import androidx.glance.appwidget.updateAll
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharingStarted
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.stateIn
import kotlinx.coroutines.launch
import java.io.File
import java.io.IOException
import java.time.LocalDate
import java.time.ZoneId
import java.time.format.DateTimeFormatter

data class EtatSynchro(
    val enCours: Boolean = false,
    val message: String? = null,
    val connecte: Boolean = false,
)

data class EtatDocuments(
    /** Pile des dossiers ouverts : (id, nom). Le premier est « Pièces jointes Gmail ». */
    val pile: List<Pair<String, String>> = listOf(Config.DOSSIER_PIECES_JOINTES to "Pièces jointes"),
    val contenu: List<DriveFichier> = emptyList(),
    val recherche: String = "",
    val chargement: Boolean = false,
    val erreur: String? = null,
    val telechargement: String? = null,
)

class AppViewModel(application: Application) : AndroidViewModel(application) {

    private val app = application as TdbApp
    private val depot = app.depot
    private val auth: GoogleAuth = app.auth
    private val api = app.api

    val vue: StateFlow<VueTableau> =
        depot.vue.stateIn(viewModelScope, SharingStarted.Eagerly, VueTableau())
    val questions: StateFlow<List<QuestionEntite>> =
        depot.questions.stateIn(viewModelScope, SharingStarted.Eagerly, emptyList())

    private val _synchro = MutableStateFlow(EtatSynchro())
    val synchro: StateFlow<EtatSynchro> = _synchro.asStateFlow()

    /** Écran de consentement Google à afficher (première fois, ou autorisation expirée). */
    private val _consentement = MutableStateFlow<PendingIntent?>(null)
    val consentement: StateFlow<PendingIntent?> = _consentement.asStateFlow()

    private val _agenda = MutableStateFlow<List<EvenementAgenda>?>(null)
    val agenda: StateFlow<List<EvenementAgenda>?> = _agenda.asStateFlow()

    private val _docs = MutableStateFlow(EtatDocuments())
    val docs: StateFlow<EtatDocuments> = _docs.asStateFlow()

    /** PDF ouvert dans la visionneuse intégrée. */
    private val _pdf = MutableStateFlow<Pair<File, String>?>(null)
    val pdf: StateFlow<Pair<File, String>?> = _pdf.asStateFlow()

    // ---------- Synchronisation ----------

    fun synchroniser(activity: Activity) {
        if (_synchro.value.enCours) return
        _synchro.value = _synchro.value.copy(enCours = true, message = null)
        viewModelScope.launch {
            when (val r = auth.autoriser(activity)) {
                is GoogleAuth.Resultat.Ok -> faireSynchro()
                is GoogleAuth.Resultat.ConsentementRequis -> {
                    _consentement.value = r.pendingIntent
                    _synchro.value = _synchro.value.copy(enCours = false)
                }
                is GoogleAuth.Resultat.Erreur -> _synchro.value =
                    EtatSynchro(message = "Connexion Google impossible : ${r.message}")
            }
        }
    }

    fun consentementLance() {
        _consentement.value = null
    }

    fun apresConsentement(activity: Activity, data: Intent?) {
        when (val r = auth.depuisIntent(activity, data)) {
            is GoogleAuth.Resultat.Ok -> {
                _synchro.value = _synchro.value.copy(enCours = true, message = null)
                viewModelScope.launch { faireSynchro() }
            }
            is GoogleAuth.Resultat.Erreur -> _synchro.value = EtatSynchro(message = r.message)
            is GoogleAuth.Resultat.ConsentementRequis -> _consentement.value = r.pendingIntent
        }
    }

    private suspend fun faireSynchro() {
        try {
            val bilan = depot.synchroniser(api)
            chargerAgenda()
            if (_docs.value.contenu.isEmpty()) chargerDossierCourant()
            majWidget()
            val msg = when {
                bilan.nouvelles.isNotEmpty() -> "Nouvelle synthèse : " + bilan.nouvelles.joinToString(" et ") { nomBoite(it) }
                else -> null
            }
            _synchro.value = EtatSynchro(enCours = false, message = msg, connecte = true)
        } catch (e: Exception) {
            _synchro.value = EtatSynchro(enCours = false, message = messageErreur(e))
        }
    }

    private suspend fun chargerAgenda() {
        val zone = ZoneId.systemDefault()
        val debut = LocalDate.now().atStartOfDay(zone).toOffsetDateTime()
        val fin = debut.plusDays(30)
        val f = DateTimeFormatter.ISO_OFFSET_DATE_TIME
        _agenda.value = api.evenements(f.format(debut), f.format(fin))
    }

    private suspend fun majWidget() {
        runCatching { TdbWidget().updateAll(getApplication<Application>()) }
    }

    // ---------- Tâches et questions ----------

    fun marquer(id: String, statut: String, nouvelleEcheance: String? = null, note: String? = null) {
        viewModelScope.launch {
            depot.marquer(id, statut, nouvelleEcheance, note)
            majWidget()
            envoyerSiConnecte()
        }
    }

    fun annuler(id: String) {
        viewModelScope.launch {
            depot.annuler(id)
            majWidget()
            envoyerSiConnecte()
        }
    }

    fun poserQuestion(boite: String?, texte: String) {
        if (texte.isBlank()) return
        viewModelScope.launch {
            depot.poserQuestion(boite, texte)
            envoyerSiConnecte()
        }
    }

    /** Envoie retours.json tout de suite si le jeton est encore valable, sinon à la prochaine ouverture. */
    private suspend fun envoyerSiConnecte() {
        if (auth.jeton == null) return
        try {
            depot.synchroniser(api)
        } catch (e: Exception) {
            if (e is JetonInvalide) auth.oublierJeton()
        }
    }

    // ---------- Documents ----------

    fun chargerDossierCourant() {
        val (id, _) = _docs.value.pile.last()
        _docs.value = _docs.value.copy(chargement = true, erreur = null, recherche = "")
        viewModelScope.launch {
            try {
                val contenu = api.enfants(id)
                _docs.value = _docs.value.copy(contenu = contenu, chargement = false)
            } catch (e: Exception) {
                _docs.value = _docs.value.copy(chargement = false, erreur = messageErreur(e))
            }
        }
    }

    fun ouvrirDossier(f: DriveFichier) {
        _docs.value = _docs.value.copy(pile = _docs.value.pile + (f.id to f.name))
        chargerDossierCourant()
    }

    fun remonter(): Boolean {
        val pile = _docs.value.pile
        if (pile.size <= 1 && _docs.value.recherche.isEmpty()) return false
        _docs.value = _docs.value.copy(pile = if (pile.size > 1) pile.dropLast(1) else pile)
        chargerDossierCourant()
        return true
    }

    fun chercher(texte: String) {
        if (texte.isBlank()) {
            chargerDossierCourant()
            return
        }
        _docs.value = _docs.value.copy(chargement = true, erreur = null, recherche = texte)
        viewModelScope.launch {
            try {
                _docs.value = _docs.value.copy(contenu = api.chercher(texte.trim()), chargement = false)
            } catch (e: Exception) {
                _docs.value = _docs.value.copy(chargement = false, erreur = messageErreur(e))
            }
        }
    }

    /** Télécharge un fichier Drive puis le passe à l'écran (aperçu PDF ou autre appli). */
    fun telecharger(f: DriveFichier, quandPret: (File) -> Unit) {
        _docs.value = _docs.value.copy(telechargement = f.name, erreur = null)
        viewModelScope.launch {
            try {
                val fichier = depot.ouvrirDocument(api, f)
                _docs.value = _docs.value.copy(telechargement = null)
                quandPret(fichier)
            } catch (e: Exception) {
                _docs.value = _docs.value.copy(telechargement = null, erreur = messageErreur(e))
            }
        }
    }

    fun ouvrirPdf(fichier: File, titre: String) {
        _pdf.value = fichier to titre
    }

    fun fermerPdf() {
        _pdf.value = null
    }

    // ---------- Divers ----------

    private fun messageErreur(e: Exception): String = when (e) {
        is JetonInvalide -> {
            auth.oublierJeton()
            "Connexion Google expirée : touche Actualiser."
        }
        is ErreurApi -> if (e.code == 403) "Google refuse l'accès (403). Vérifie les autorisations." else (e.message ?: "Erreur Google")
        is IOException -> "Pas de connexion internet. Affichage de la dernière synthèse."
        else -> e.message ?: e.javaClass.simpleName
    }

    companion object {
        fun nomBoite(b: String) = when (b) {
            "gmail" -> "Gmail"
            "laposte" -> "La Poste"
            else -> b
        }
    }
}
