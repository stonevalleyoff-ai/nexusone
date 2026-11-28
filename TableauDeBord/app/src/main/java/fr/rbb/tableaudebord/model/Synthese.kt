package fr.rbb.tableaudebord.model

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject

/** Fichier écrit chaque matin par Claude dans le dossier Drive « Tableau de bord ». */
@Serializable
data class Synthese(
    val version: Int = 1,
    val boite: String = "",
    @SerialName("genere_le") val genereLe: String? = null,
    val taches: List<Tache> = emptyList(),
    val echeances: List<Echeance> = emptyList(),
    val documents: List<DocumentLie> = emptyList(),
    val conseils: List<Conseil> = emptyList(),
    val bilan: JsonObject? = null,
)

@Serializable
data class Tache(
    val id: String,
    val titre: String,
    val action: String? = null,
    val echeance: String? = null,
    val priorite: String? = null,
    val statut: String? = null,
    val source: Source? = null,
)

@Serializable
data class Source(
    val expediteur: String? = null,
    val objet: String? = null,
    val date: String? = null,
    val uid: Long? = null,
    val fil: String? = null,
)

@Serializable
data class Echeance(
    val titre: String,
    val debut: String? = null,
    val fin: String? = null,
    val date: String? = null,
    @SerialName("agenda_id") val agendaId: String? = null,
) {
    /** Date du jour concerné (AAAA-MM-JJ), quel que soit le format. */
    val jour: String? get() = date ?: debut?.take(10)
}

@Serializable
data class DocumentLie(
    val titre: String,
    @SerialName("drive_id") val driveId: String? = null,
    val type: String? = null,
    val date: String? = null,
)

@Serializable
data class Conseil(
    val id: String? = null,
    val titre: String,
    val texte: String? = null,
    @SerialName("lie_a") val lieA: String? = null,
)

/** Fichier retours.json écrit par l'appli et lu par Claude au tri suivant. */
@Serializable
data class Retours(
    val version: Int = 1,
    @SerialName("maj_le") val majLe: String,
    val taches: List<RetourTache> = emptyList(),
    val questions: List<Question> = emptyList(),
)

@Serializable
data class RetourTache(
    val id: String,
    /** "fait", "ignore", "reporte" (voir nouvelle_echeance) ou "note" (tâche gardée, note à lire). */
    val statut: String,
    @SerialName("nouvelle_echeance") val nouvelleEcheance: String? = null,
    val note: String? = null,
    val le: String,
)

@Serializable
data class Question(
    val id: String,
    /** null = pour les deux boîtes. */
    val boite: String? = null,
    val texte: String,
    val le: String,
)

val JsonTdb = Json {
    ignoreUnknownKeys = true
    isLenient = true
    explicitNulls = false
    encodeDefaults = true
    coerceInputValues = true
}
