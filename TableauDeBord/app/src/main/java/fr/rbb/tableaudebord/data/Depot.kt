package fr.rbb.tableaudebord.data

import android.content.Context
import fr.rbb.tableaudebord.Config
import fr.rbb.tableaudebord.model.Conseil
import fr.rbb.tableaudebord.model.DocumentLie
import fr.rbb.tableaudebord.model.Echeance
import fr.rbb.tableaudebord.model.JsonTdb
import fr.rbb.tableaudebord.model.Question
import fr.rbb.tableaudebord.model.RetourTache
import fr.rbb.tableaudebord.model.Retours
import fr.rbb.tableaudebord.model.Synthese
import fr.rbb.tableaudebord.model.Tache
import fr.rbb.tableaudebord.net.DriveFichier
import fr.rbb.tableaudebord.net.ErreurApi
import fr.rbb.tableaudebord.net.GoogleApi
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.combine
import java.io.File
import java.time.Instant
import java.time.LocalDate
import java.time.OffsetDateTime
import java.time.ZoneId
import java.util.UUID

/** Une tâche telle qu'affichée : la tâche de Claude + ce que tu en as fait localement. */
data class TacheVue(
    val tache: Tache,
    val boite: String,
    val etat: EtatTacheEntite?,
) {
    val echeanceEffective: String? get() = etat?.nouvelleEcheance ?: tache.echeance
    val terminee: Boolean get() = etat?.statut == "fait" || etat?.statut == "ignore"
    val rangPriorite: Int get() = when (tache.priorite) {
        "haute" -> 0
        "moyenne" -> 1
        else -> 2
    }
}

data class PassageClaude(val boite: String, val genereLe: OffsetDateTime?, val bilan: String?) {
    val perime: Boolean
        get() = genereLe == null ||
            genereLe.plusHours(Config.SYNTHESE_PERIMEE_HEURES).isBefore(OffsetDateTime.now())
}

data class VueTableau(
    val taches: List<TacheVue> = emptyList(),
    val echeances: List<Pair<Echeance, String>> = emptyList(),
    val documents: List<Pair<DocumentLie, String>> = emptyList(),
    val conseils: List<Pair<Conseil, String>> = emptyList(),
    val passages: List<PassageClaude> = emptyList(),
) {
    val aTraiter: List<TacheVue> get() = taches.filterNot { it.terminee }
}

class Depot(private val context: Context, private val dao: TdbDao) {

    private val prefs = context.getSharedPreferences("tdb_prefs", Context.MODE_PRIVATE)

    val vue: Flow<VueTableau> = combine(dao.syntheses(), dao.etats()) { syntheses, etats ->
        construireVue(syntheses, etats)
    }

    val questions: Flow<List<QuestionEntite>> = dao.questions()

    suspend fun vueMaintenant(): VueTableau = construireVue(dao.synthesesMaintenant(), dao.etatsMaintenant())

    private fun construireVue(syntheses: List<SyntheseEntite>, etats: List<EtatTacheEntite>): VueTableau {
        val parId = etats.associateBy { it.id }
        val lues = syntheses.mapNotNull { e ->
            runCatching { JsonTdb.decodeFromString(Synthese.serializer(), e.json) }.getOrNull()
                ?.let { e.boite to it }
        }
        val aujourdHui = LocalDate.now().toString()
        val taches = lues.flatMap { (boite, s) -> s.taches.map { TacheVue(it, boite, parId[it.id]) } }
            .sortedWith(
                compareBy<TacheVue> { it.terminee }
                    .thenBy { it.echeanceEffective ?: "9999" }
                    .thenBy { it.rangPriorite },
            )
        val echeances = lues.flatMap { (boite, s) -> s.echeances.map { it to boite } }
            .filter { (it.first.jour ?: "9999") >= aujourdHui }
            .sortedBy { it.first.debut ?: it.first.jour ?: "9999" }
        val documents = lues.flatMap { (boite, s) -> s.documents.map { it to boite } }
            .sortedByDescending { it.first.date ?: "" }
        val conseils = lues.flatMap { (boite, s) -> s.conseils.map { it to boite } }
        val passages = lues.map { (boite, s) ->
            PassageClaude(
                boite = boite,
                genereLe = s.genereLe?.let { runCatching { OffsetDateTime.parse(it) }.getOrNull() },
                bilan = s.bilan?.toString(),
            )
        }.sortedBy { it.boite }
        return VueTableau(taches, echeances, documents, conseils, passages)
    }

    // ---------- Synchronisation avec Drive ----------

    data class BilanSynchro(val nouvelles: List<String>, val retoursEnvoyes: Boolean)

    suspend fun synchroniser(api: GoogleApi): BilanSynchro {
        val nouvelles = mutableListOf<String>()
        val actuelles = dao.synthesesMaintenant().associateBy { it.boite }
        for (boite in Config.BOITES) {
            val prefixe = Config.prefixeSynthese(boite)
            val dernier: DriveFichier = api.listerDrive(
                "'${Config.DOSSIER_TABLEAU_DE_BORD}' in parents and name contains '$prefixe' and trashed = false",
                "name desc",
            ).filter { it.name.startsWith(prefixe) && it.name.endsWith(".json") }
                .maxByOrNull { it.name } ?: continue
            if (actuelles[boite]?.nomFichier == dernier.name) continue
            val json = api.telechargerTexte(dernier.id)
            // On vérifie que le fichier se lit avant de remplacer l'ancien.
            JsonTdb.decodeFromString(Synthese.serializer(), json)
            dao.enregistrerSynthese(SyntheseEntite(boite, dernier.name, json, System.currentTimeMillis()))
            nouvelles += boite
        }
        val envoyes = if (prefs.getBoolean(CLE_RETOURS_SALES, false)) {
            envoyerRetours(api)
            true
        } else {
            false
        }
        prefs.edit().putLong(CLE_DERNIERE_SYNCHRO, System.currentTimeMillis()).apply()
        return BilanSynchro(nouvelles, envoyes)
    }

    val derniereSynchro: Long get() = prefs.getLong(CLE_DERNIERE_SYNCHRO, 0L)
    val retoursEnAttente: Boolean get() = prefs.getBoolean(CLE_RETOURS_SALES, false)

    private suspend fun envoyerRetours(api: GoogleApi) {
        val limite = System.currentTimeMillis() - TRENTE_JOURS_MS
        dao.purgerEtats(limite)
        dao.purgerQuestions(limite)
        val retours = Retours(
            majLe = OffsetDateTime.now().toString(),
            taches = dao.etatsDepuis(limite).map {
                RetourTache(it.id, it.statut, it.nouvelleEcheance, it.note, iso(it.le))
            },
            questions = dao.questionsDepuis(limite).map { Question(it.id, it.boite, it.texte, iso(it.le)) },
        )
        val contenu = JsonTdb.encodeToString(Retours.serializer(), retours)

        var id = prefs.getString(CLE_RETOURS_ID, null)
            ?: api.listerDrive(
                "'${Config.DOSSIER_TABLEAU_DE_BORD}' in parents and name = '${Config.FICHIER_RETOURS}' and trashed = false",
            ).firstOrNull()?.id
        val majOk = id != null && try {
            api.majJson(id, contenu)
            true
        } catch (e: ErreurApi) {
            // 403/404 : fichier supprimé ou créé par quelqu'un d'autre que l'appli.
            if (e.code == 403 || e.code == 404) false else throw e
        }
        if (!majOk) {
            id = api.creerJson(Config.DOSSIER_TABLEAU_DE_BORD, Config.FICHIER_RETOURS, contenu)
        }
        prefs.edit()
            .putString(CLE_RETOURS_ID, id)
            .putBoolean(CLE_RETOURS_SALES, false)
            .apply()
    }

    // ---------- Actions locales ----------

    suspend fun marquer(id: String, statut: String, nouvelleEcheance: String? = null, note: String? = null) {
        val ancien = dao.etatsMaintenant().firstOrNull { it.id == id }
        dao.enregistrerEtat(
            EtatTacheEntite(
                id = id,
                statut = statut,
                nouvelleEcheance = nouvelleEcheance ?: ancien?.nouvelleEcheance,
                note = note ?: ancien?.note,
                le = System.currentTimeMillis(),
            ),
        )
        marquerSale()
    }

    suspend fun annuler(id: String) {
        dao.effacerEtat(id)
        marquerSale()
    }

    suspend fun poserQuestion(boite: String?, texte: String) {
        dao.enregistrerQuestion(QuestionEntite(UUID.randomUUID().toString(), boite, texte.trim(), System.currentTimeMillis()))
        marquerSale()
    }

    private fun marquerSale() {
        prefs.edit().putBoolean(CLE_RETOURS_SALES, true).apply()
    }

    // ---------- Documents ----------

    fun fichierCache(f: DriveFichier): File {
        val nomPropre = f.name.replace(Regex("[^A-Za-z0-9._ -]"), "_").take(80)
        return File(File(context.cacheDir, "docs"), "${f.id}_$nomPropre")
    }

    suspend fun ouvrirDocument(api: GoogleApi, f: DriveFichier): File {
        val cible = fichierCache(f)
        if (!cible.exists() || cible.length() == 0L) api.telechargerVers(f.id, cible)
        return cible
    }

    private fun iso(ms: Long): String =
        OffsetDateTime.ofInstant(Instant.ofEpochMilli(ms), ZoneId.systemDefault()).toString()

    companion object {
        private const val CLE_RETOURS_ID = "retours_id"
        private const val CLE_RETOURS_SALES = "retours_sales"
        private const val CLE_DERNIERE_SYNCHRO = "derniere_synchro"
        private const val TRENTE_JOURS_MS = 30L * 24 * 3600 * 1000
    }
}
